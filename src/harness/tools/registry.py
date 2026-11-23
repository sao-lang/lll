"""R5 工具注册与统一调用：ToolPort 的默认实现 ToolRegistry。

核心纪律（R9 §5 权威路由）：
- danger 以注册 spec 为准（忽略 call.danger 防伪造降级）；
- SAFE 直执行 / LOW 审计后直执行 / HIGH 沙盒 / CRITICAL 审批后沙盒；
- 工具、沙盒、审计、审批的异常一律折叠为失败 ToolResult，不抛穿；
- L1 目录绝不含完整 parameters_schema（渐进披露红线）。
"""
from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Self

from compose import (
    AuditPort, DangerLevel, HumanPort, PortCtx, SandboxPort,
    ToolCall, ToolPort, ToolResult, ToolSpec,
)
from runtime import DynamicValue

from .decorator import clear_pending, pending_tools

__all__ = ["ToolRegistry"]

# 稳定错误码与比较常量（规则 5：比较右侧必须是常量名而非字符串字面量）。
_ERR_NOT_FOUND = "NOT_FOUND"
_ERR_INVALID_ARGS = "INVALID_ARGS"
_ERR_NO_SANDBOX = "NO_SANDBOX"
_ERR_NO_HUMAN = "NO_HUMAN"
_ERR_APPROVAL_DENIED = "APPROVAL_DENIED"
_ERR_NO_AUDIT = "NO_AUDIT"
_ERR_TOOL_ERROR = "TOOL_ERROR"
_APPROVE = "approve"
_OPT_DENY = "deny"
_KEY_PROPERTIES = "properties"


class ToolRegistry:
    """工具注册表：注册/目录/schema/静态检查/调用四路由的唯一入口。

    属性：
        name: port 名（装配期按 name 校验）。
    """

    name: str
    _specs: dict[str, ToolSpec]
    _fns: dict[str, Callable[..., DynamicValue]]
    _sandbox: SandboxPort | None
    _human: HumanPort | None
    _audit: AuditPort | None
    _frozen: bool

    def __init__(
        self,
        *,
        sandbox: SandboxPort | None = None,
        human: HumanPort | None = None,
        audit: AuditPort | None = None,
        name: str = "tools",
    ) -> None:
        """构造注册表：三个高危路由依赖可缺省注入，缺席时按 fail-closed 处理。"""
        self.name = name
        self._specs = {}
        self._fns = {}
        self._sandbox = sandbox
        self._human = human
        self._audit = audit
        self._frozen = False

    def register(self, spec: ToolSpec, fn: Callable[..., DynamicValue]) -> None:
        """注册工具（重名拒绝）；danger=None 允许注册，由 static_check 暴露问题。

        参数：
            spec: 工具目录条目。fn: 接受 **kwargs 的执行体（可同步可异步）。
        """
        if self._frozen:  # 冻结后目录不再变更，防止运行期工具集漂移。
            raise RuntimeError(f"registry {self.name} is frozen")
        if spec.name in self._specs:
            # 重名在注册期就挡住，避免调用路由出现二义性。
            raise ValueError(f"duplicate tool: {spec.name}")
        # 不在 register 拒绝 danger=None：装配期需要先注册再由 static_check 统一报告。
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn

    def register_pending(self) -> list[str]:
        """装配期批量注册 @tool 待注册项，成功即清空容器（v2.2 自动注册）。

        为何先全量预检再批量注册：预检覆盖 register 的全部失败条件（freeze 状态与重名），
        确认整批可落地后才逐个写入，避免"注册一半抛错"留下半状态；预检通过后批量阶段
        不会再抛，故无需回滚。失败时容器保留，修正后可重试。

        Returns:
            按装饰顺序注册的工具名列表（无待注册项时为空列表）。

        Raises:
            RuntimeError: registry 已冻结，整批拒绝。
            ValueError: 待注册项之间或与本 registry 已有工具重名，整批拒绝。
        """
        pending = pending_tools()
        if not pending:
            return []
        if self._frozen:  # 与 register 的冻结纪律一致，先于重名检查。
            raise RuntimeError(f"registry {self.name} is frozen")
        seen: set[str] = set(self._specs)  # 已有工具名参与重名判定。
        for tool_fn in pending:  # 全量预检：容器内部重名与外部重名一并暴露。
            if tool_fn.spec.name in seen:
                raise ValueError(f"duplicate tool: {tool_fn.spec.name}")
            seen.add(tool_fn.spec.name)
        names: list[str] = []
        for tool_fn in pending:  # 预检已排除所有失败条件，批量阶段不会再抛。
            self.register(tool_fn.spec, tool_fn)
            names.append(tool_fn.spec.name)
        clear_pending()  # 成功即清空；失败在上面已提前返回/抛出，不会走到这里。
        return names

    def bind(self, name: str, fn: Callable[..., DynamicValue]) -> None:
        """延迟绑定/替换已注册工具的执行体；未注册则 KeyError。"""
        if name not in self._fns:
            # 只允许替换已知工具，防止绕过 register 偷偷注入实现。
            raise KeyError(name)
        self._fns[name] = fn

    def catalog(self, level: int = 1) -> list[ToolSpec]:
        """按注册顺序返回目录；level=1 清空 schema，level>=2 返回完整 spec。"""
        if level <= 1:
            # L1 红线：浅拷贝并清空 parameters_schema，避免披露完整 schema。
            return [s.model_copy(update={"parameters_schema": {}}) for s in self._specs.values()]
        return list(self._specs.values())

    def schema_of(self, name: str) -> dict[str, DynamicValue]:
        """按名返回完整 parameters_schema（未注册 KeyError）；返回副本防外部改写。"""
        if name not in self._specs:
            raise KeyError(name)
        return dict(self._specs[name].parameters_schema)

    def static_check(self) -> list[str]:
        """装配期静态检查：danger 缺失、高危无沙盒、CRITICAL 无人工，逐条列出。"""
        problems: list[str] = []
        for name, spec in self._specs.items():  # 逐工具检查，收集全部问题而非首个即止。
            if spec.danger is None:
                problems.append(f"{name}: danger not declared")
                continue
            resolved = DangerLevel.coerce(spec.danger)
            if resolved in (DangerLevel.HIGH, DangerLevel.CRITICAL):
                # 高危工具没有沙盒等于裸奔，必须在装配期暴露。
                if self._sandbox is None:
                    problems.append(f"{name}: {resolved.value} tool missing sandbox")
                # CRITICAL 还需人工闸门，沙盒在场也不能免。
                if resolved is DangerLevel.CRITICAL and self._human is None:
                    problems.append(f"{name}: CRITICAL tool missing human reviewer")
        return problems

    def freeze(self) -> Self:
        """冻结目录（之后 register 抛 RuntimeError）；bind 仍可用于热更执行体。"""
        self._frozen = True
        return self

    async def _maybe_await(self, value: DynamicValue) -> DynamicValue:
        """同步/异步执行体归一：awaitable 才 await（避免 asyncio.iscoroutine 推 Any）。"""
        if inspect.isawaitable(value):
            return await value
        return value

    async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:
        """统一调用入口：四级 danger 路由 + 参数校验 + 异常折叠 + 耗时记录。"""
        started = time.perf_counter()
        initial = DangerLevel.coerce(call.danger)
        meta: dict[str, DynamicValue] = {
            "call_id": call.call_id,
            "danger": initial.value,
            "source": call.source,
            "sandbox": False,
            "approved": False,
            "retryable": True,
        }

        def finish(
            value: DynamicValue = None, *, ok: bool = True, error: str | None = None,
        ) -> ToolResult:
            # 为何：所有出口统一补耗时；复制 meta 防止调用方改动注册表内部状态。
            meta["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            # 成功/沙盒审批通过可重试，失败不可盲目重试：统一按 ok 落 retryable。
            meta["retryable"] = ok
            return ToolResult(
                call_id=call.call_id, ok=ok, value=value, error=error, meta=dict(meta),
            )

        if call.name not in self._specs:
            # 未注册：折叠为 NOT_FOUND，不抛 KeyError。
            return finish(ok=False, error=_ERR_NOT_FOUND)

        spec = self._specs[call.name]
        level = DangerLevel.coerce(spec.danger)  # 以注册 spec 为准，忽略伪造的 call.danger。
        meta["danger"] = level.value
        meta["source"] = spec.source
        fn = self._fns[call.name]

        if _KEY_PROPERTIES in spec.parameters_schema:
            # 仅当 schema 显式声明 properties 才做 extra=forbid：
            # 无该键（如 MCP 的 {"type":"object"}）视为不约束，避免误伤外部工具。
            props = spec.parameters_schema[_KEY_PROPERTIES]
            allowed: set[str] = set(props) if isinstance(props, dict) else set()
            for key in call.args:  # 命中第一个非法键即拒绝，错误信息保持稳定。
                if key not in allowed:
                    return finish(ok=False, error=_ERR_INVALID_ARGS)

        # 审计事件单一字段契约（B1 修复）：写入方与 AuditLogger 消费方共用一套具名字段，
        # danger/tool/call_id/source 等语义并入 rationale，避免"链有效但内容空壳"。
        async def record_audit(*, approved: bool, sandboxed: bool) -> None:
            # 调用前置检查保证非 SAFE 分支 _audit 非 None；这里显式断言而非静默 return，
            # 防止未来新增分支漏掉前置检查时静默放行（旧降级标记语义复活）。
            assert self._audit is not None, "record_audit called without audit port"
            await self._audit.record({
                "who": ctx.session_id,
                "when": int(time.time()),
                "action": f"tool.{level.value.lower()}.call",
                "cost": 0.0,
                "data_touched": (call.name, call.call_id),
                "rationale": (f"danger={level.value};source={spec.source};"
                              f"tool={call.name};call_id={call.call_id};"
                              f"approved={approved};sandbox={sandboxed}"),
            }, ctx)

        try:
            if level is DangerLevel.SAFE:
                # SAFE：无副作用顾虑，直接执行（同步函数经 _maybe_await 归一）。
                return finish(await self._maybe_await(fn(**call.args)))
            if self._audit is None:
                # LOW/HIGH/CRITICAL 全部要求审计（R9 §5 审计覆盖率 100%）。
                return finish(ok=False, error=_ERR_NO_AUDIT)
            if level is DangerLevel.LOW:
                # LOW：先记审计再直执行。
                await record_audit(approved=False, sandboxed=False)
                return finish(await self._maybe_await(fn(**call.args)))
            if level is DangerLevel.HIGH:
                # HIGH：必须沙盒；缺席即拒绝，绝不降级为直执行。
                if self._sandbox is None:
                    await record_audit(approved=False, sandboxed=False)
                    return finish(ok=False, error=_ERR_NO_SANDBOX)
                out = await self._sandbox.run(fn, call.args, ctx)
                meta["sandbox"] = True
                await record_audit(approved=False, sandboxed=True)
                return finish(out)
            # CRITICAL：审批 + 沙盒两道门缺一不可；每条决策出口都写审计。
            if self._human is None:
                await record_audit(approved=False, sandboxed=False)
                return finish(ok=False, error=_ERR_NO_HUMAN)
            question = f"Approve execution of tool {call.name} (call_id={call.call_id})?"
            # S4：把 danger 透传给审批包装器（经 ctx.meta，兼容 HumanPort 三参签名）。
            ctx.meta["danger"] = level.value
            answer = await self._human.ask(question, [_APPROVE, _OPT_DENY], ctx)
            if answer.strip().lower() != _APPROVE:
                # 右侧是模块常量（Name），不是字符串字面量比较（规则 5）。
                await record_audit(approved=False, sandboxed=False)
                return finish(ok=False, error=_ERR_APPROVAL_DENIED)
            if self._sandbox is None:
                await record_audit(approved=True, sandboxed=False)
                return finish(ok=False, error=_ERR_NO_SANDBOX)
            out = await self._sandbox.run(fn, call.args, ctx)
            meta["approved"] = True
            meta["sandbox"] = True
            await record_audit(approved=True, sandboxed=True)
            return finish(out)
        except Exception as exc:
            # 工具/沙盒/审计/审批任一异常都折叠为失败信封，不抛穿到调用方。
            return finish(ok=False, error=f"{_ERR_TOOL_ERROR}: {exc}")
