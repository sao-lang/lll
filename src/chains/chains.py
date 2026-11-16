"""Chains：七挂载点 + 四泳道的统一洋葱管道（M2）。

四泳道语义：
- middleware：包裹执行（async(ctx, next)），可短路但必须声明；
- filter：判决 allow/redact/reject/retry/skip/degrade/abort（含步骤级错误处置）；
- pipes：纯数据变换（去程 payload / 回程 result.data）；
- hooks：只读观察，异常吞掉但留名进 trace。

链路自己不碰外部世界：洋葱中心调用 `ctx.invoke`（由 Runner 注入 M3 的 call）。
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast

from runtime import DynamicValue, Effect, EffectKind, EffectPayload, Result, Session

from .types import (
    ChainConfigError,
    ChainCtx,
    ChainEvent,
    DispatchResult,
    MountPoint,
    Verdict,
    require_name,
)

__all__ = ["Chains", "MOUNT_GROUP"]

MOUNT_GROUP: dict[EffectKind, tuple[MountPoint, MountPoint]] = {
    EffectKind.LLM: (MountPoint.BEFORE_LLM, MountPoint.AFTER_LLM),
    EffectKind.TOOL: (MountPoint.BEFORE_TOOL, MountPoint.AFTER_TOOL),
    # 其余 kind 无专用挂载点：走"进出两级"默认组（M8 两级护栏落点）
}

DEFAULT_GROUP = (MountPoint.ON_INPUT, MountPoint.ON_OUTPUT)


async def _maybe(value: DynamicValue) -> DynamicValue:
    """统一等待：组件钩子既可是同步值也可是 awaitable。"""
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class _Component:
    """已注册的单个链路组件；name 供留名，obj 是实际对象。"""

    name: str  # 组件名（装配期校验非空）
    kind: str  # 泳道类别：middleware / filter / pipe / hook
    at: MountPoint | None  # 挂载点；None 表示全挂载点生效
    obj: DynamicValue  # 组件实例
    allow_short_circuit: bool = False  # 是否允许不调 nxt（仅 middleware）


class Chains:
    """链路注册表 + 调度器；Runner 注入的 dispatch 即本类 .dispatch。"""

    def __init__(self) -> None:
        self._components: list[_Component] = []  # 按注册顺序存放的组件
        self._frozen = False  # freeze() 后禁止再注册
        self.required_ports: frozenset[str] = frozenset()  # 装配期声明的必需 port

    # ------------------------------------------------------------------ #
    # 注册（顺序即执行顺序；先注册的 middleware 在外层）
    # ------------------------------------------------------------------ #
    def _add(self, kind: str, obj: DynamicValue, at: MountPoint | None, **flags: DynamicValue) -> Chains:
        """内部注册入口：校验留名与挂载点合法性后追加组件，返回 self 支持链式。"""
        if self._frozen:
            raise ChainConfigError("chains is frozen; register components before freeze()")
        name = require_name(obj, kind)
        mount: MountPoint | None = None
        if at is not None:
            try:
                mount = MountPoint(at)
            except ValueError as exc:
                raise ChainConfigError(f"illegal mount point: {at!r}") from exc
        self._components.append(_Component(name, kind, mount, obj, **flags))
        return self

    def use(
        self, mw: DynamicValue, *, at: MountPoint | None = None, allow_short_circuit: bool = False
    ) -> Chains:
        """注册 middleware（洋葱包裹）；at 限定挂载点，默认全挂载点生效。"""
        return self._add("middleware", mw, at, allow_short_circuit=allow_short_circuit)

    def filter(self, f: DynamicValue, *, at: MountPoint) -> Chains:
        """注册 filter 到指定挂载点；判决两阶段数据。"""
        return self._add("filter", f, at)

    def pipe(self, p: DynamicValue, *, at: MountPoint) -> Chains:
        """注册 pipe 到指定挂载点；做纯数据变换。"""
        return self._add("pipe", p, at)

    def hook(self, h: DynamicValue) -> Chains:
        """注册 hook（观察者）；hook 全挂载点生效。"""
        return self._add("hook", h, None)

    def freeze(self, required_ports: Iterable[str] = ()) -> Chains:
        """装配期检查：挂载点合法性、组件留名、组件声明的 port 依赖齐备。"""
        self.required_ports = frozenset(required_ports)
        for comp in self._components:
            require_name(comp.obj, comp.kind)
            for port in getattr(comp.obj, "requires_ports", ()):
                if port not in self.required_ports:
                    raise ChainConfigError(
                        f"component {comp.name} requires port {port!r}, not registered"
                    )
        self._frozen = True
        return self

    def ctx_factory(self) -> Callable[[Effect, Session], ChainCtx]:
        """给 Runner 注入的上下文工厂：产生 M2 的 ChainCtx（RuntimeCtx 的超集）。"""
        def factory(effect: Effect, session: Session) -> ChainCtx:
            """按 effect.kind 选取去程挂载点，产出 ChainCtx。"""
            req, _ = MOUNT_GROUP.get(effect.kind, DEFAULT_GROUP)
            return ChainCtx(
                effect=effect,
                state_view=session.state,
                session_id=session.session_id,
                mount=req,
                budget_left=session.state.budgets,
            )
        return factory

    # ------------------------------------------------------------------ #
    # 内部泳道
    # ------------------------------------------------------------------ #
    def _select(self, kind: str, mount: MountPoint) -> list[_Component]:
        """选出某泳道在指定挂载点生效的组件（at=None 表示全挂载点）。"""
        return [c for c in self._components if c.kind == kind and (c.at is None or c.at == mount)]

    def _pipes(self, mount: MountPoint, data: DynamicValue, ctx: ChainCtx) -> DynamicValue:
        """依次执行 pipe 变换，每执行一个就留名进 trace。"""
        for comp in self._select("pipe", mount):
            data = comp.obj.transform(data, ctx)
            ctx.trace.append(f"pipe:{comp.name}")
        return data

    def _filters(self, mount: MountPoint, phase: Literal["request", "response"], ctx: ChainCtx) -> Verdict:
        """依次执行 filter；首个非 allow 判决即短路返回，否则返回 allow。"""
        for comp in self._select("filter", mount):
            verdict = cast(Verdict, comp.obj.check(ctx, phase))
            ctx.trace.append(f"filter:{comp.name}")
            if verdict.action != "allow":
                return verdict
        return Verdict.allow()

    async def _hooks(
        self, mount: MountPoint, phase: Literal["request", "response"], ctx: ChainCtx,
        data: DynamicValue = None, error: DynamicValue = None,
    ) -> None:
        """向 hook 广播事件；单个观察者异常只记 trace，绝不影响主流程。"""
        for comp in self._select("hook", mount):
            event = ChainEvent(
                mount=mount, phase=phase, component=comp.name,
                effect=ctx.effect, data=data, error=error, ts=ctx.attempt,
            )
            try:
                await _maybe(comp.obj.on_event(event, ctx))
                ctx.trace.append(f"hook:{comp.name}")
            except Exception as exc:  # 观察者故障绝不影响主流程
                ctx.trace.append(f"hook:{comp.name}:error:{type(exc).__name__}")

    async def _onion(
        self, ctx: ChainCtx, req: MountPoint, resp: MountPoint,
        core_fn: Callable[[], DynamicValue] | None = None,
    ) -> Result:
        """洋葱调度：middleware 由外到内包裹 core，逐层检测未声明短路。

        core 默认调 ctx.invoke（M3 call）；中间件返回 None 表示透传内层结果。
        """
        # middleware 覆盖整组挂载点（去程 + 回程），不能只按 req 过滤
        mws = [
            c for c in self._components
            if c.kind == "middleware" and (c.at is None or c.at in (req, resp))
        ]

        async def core() -> Result:
            """洋葱中心：自定义 core（纯挂载点）或调用注入的 M3 port。"""
            if core_fn is not None:
                return cast(Result, core_fn())
            if ctx.invoke is None:
                return Result.failure("INVOKE_MISSING", "ctx.invoke not bound by Runner")
            return cast(Result, await _maybe(ctx.invoke(ctx.effect, ctx)))

        async def layer(index: int) -> Result:
            """递归构造洋葱层：调用第 index 个中间件，nxt 指向下一层。"""
            if index >= len(mws):
                return await core()
            comp = mws[index]
            called = False
            box: dict[str, Result] = {}

            async def nxt() -> Result:
                """传给中间件的下一层回调；置 called 标记并缓存结果供透传。"""
                nonlocal called
                called = True
                result = await layer(index + 1)
                box["result"] = result
                return result

            try:
                out = await _maybe(comp.obj(ctx, nxt))
            except Exception as exc:
                # 中间件异常折叠为 Result，交回程 filter 判决（绝不抛穿运行时）
                ctx.trace.append(f"middleware:{comp.name}:error:{type(exc).__name__}")
                return Result.failure("MIDDLEWARE_ERROR", f"{comp.name}: {exc}")
            ctx.trace.append(f"middleware:{comp.name}")
            if not called and not comp.allow_short_circuit:
                ctx.trace.append(f"middleware:{comp.name}:undeclared-short-circuit")
                return Result.failure(
                    "MW_SHORT_CIRCUIT", f"{comp.name} did not await next()"
                )
            if out is None:
                return box.get("result", Result.success(None))
            return out if isinstance(out, Result) else Result.success(out)

        return await layer(0)

    def _pack(self, ctx: ChainCtx, verdict: Verdict, data: Result | None,
              abort: bool = False) -> DispatchResult:
        """把当前上下文的 trace 与判决打包为 DispatchResult。"""
        return DispatchResult(
            effect=ctx.effect, verdict=verdict, data=data,
            trace=tuple(ctx.trace), abort=abort,
        )

    # ------------------------------------------------------------------ #
    # 公开执行入口
    # ------------------------------------------------------------------ #
    async def dispatch(self, effect: Effect, ctx: ChainCtx) -> DispatchResult:
        """一次 Effect 的完整链路：去程 → 洋葱 → 回程 → hooks。"""
        req, resp = MOUNT_GROUP.get(effect.kind, DEFAULT_GROUP)
        ctx.mount = req
        ctx.trace.append(f"mount:{req.value}")

        payload = self._pipes(req, dict(effect.payload), ctx)
        ctx.effect = replace(
            effect,
            payload=cast(EffectPayload, payload if isinstance(payload, Mapping) else {}),
        )
        await self._hooks(req, "request", ctx, data=payload)

        verdict = self._filters(req, "request", ctx)
        if verdict.action == "reject":
            return self._pack(ctx, verdict, Result.failure("FILTER_REJECTED", verdict.reason))
        if verdict.action == "abort":
            return self._pack(ctx, verdict, None, abort=True)
        if verdict.action == "skip":
            return self._pack(ctx, verdict, Result.success(verdict.data))
        if verdict.action == "redact":
            if isinstance(verdict.data, Mapping):
                ctx.effect = replace(ctx.effect, payload=cast(EffectPayload, dict(verdict.data)))
            else:
                ctx.meta["request_data"] = verdict.data
        elif verdict.action == "degrade":
            ctx.meta["degrade_port"] = verdict.data

        # 回程循环：洋葱执行 → 判决；retry 会重跑，其余动作改写结果后退出
        result = Result.success(None)
        for attempt in range(64):
            ctx.attempt = attempt
            result = await self._onion(ctx, req, resp)
            ctx.meta["error"] = result.error if not result.ok else None
            ctx.mount = resp
            verdict = self._filters(resp, "response", ctx)

            if verdict.action == "retry" and attempt < verdict.retry:
                if verdict.backoff > 0:
                    await asyncio.sleep(verdict.backoff)
                continue
            if verdict.action == "skip":
                result = Result.success(verdict.data)
            elif verdict.action == "redact":
                result = Result.success(verdict.data, result.usage)
            elif verdict.action == "reject":
                result = Result.failure("FILTER_REJECTED", verdict.reason)
            elif verdict.action == "abort":
                return self._pack(ctx, verdict, result, abort=True)
            elif verdict.action == "degrade":
                ctx.meta["degrade_port"] = verdict.data
                result = await self._onion(ctx, req, resp)
            break

        # 成功结果过回程 pipe；然后广播 response hook
        if result.ok:
            result = replace(result, data=self._pipes(resp, result.data, ctx))
        await self._hooks(
            resp, "response", ctx,
            data=result.data if result.ok else None,
            error=result.error if not result.ok else None,
        )
        return self._pack(ctx, verdict, result)

    async def run_mount(self, mount: MountPoint, ctx: ChainCtx, data: DynamicValue = None) -> DispatchResult:
        """显式执行某个挂载点（ON_STATE 上下文压缩 / ON_INPUT 输入护栏等），不调 Port。"""
        ctx.mount = MountPoint(mount)
        ctx.trace.append(f"mount:{ctx.mount.value}")
        transformed = self._pipes(ctx.mount, data, ctx)
        await self._hooks(ctx.mount, "request", ctx, data=transformed)
        verdict = self._filters(ctx.mount, "request", ctx)
        if verdict.action == "reject":
            return self._pack(ctx, verdict, Result.failure("FILTER_REJECTED", verdict.reason))
        if verdict.action == "abort":
            return self._pack(ctx, verdict, None, abort=True)
        if verdict.action == "redact":
            transformed = verdict.data
        elif verdict.action == "skip":
            transformed = verdict.data
        # ON_STATE 等纯挂载点同样走 middleware 洋葱（核心不调 Port，只透传数据）
        core_result = await self._onion(
            ctx, ctx.mount, ctx.mount, lambda: Result.success(transformed)
        )
        transformed = core_result.data
        await self._hooks(ctx.mount, "response", ctx, data=transformed)
        return self._pack(ctx, verdict, core_result)
