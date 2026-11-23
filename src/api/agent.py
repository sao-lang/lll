"""语义门面 Agent —— 用户只碰 say()/run()，零 message/effect/payload/ctx 暴露。

设计（契约③④；Round1 B1-B4 + Round2 Blk-1~4 根因修复）：
- 位于 api 层（不改依赖方向：compose 不反向依赖 harness/api）；
- 按 port 分派 call：llm→gateway、tool→ToolRegistry、其余→注入的 port handler（不绕过审批/沙盒/审计）；
- 履约校验：纯签名匹配（unbound 方法、跳过非 runtime_checkable 协议），不靠 isinstance；
- 注入的 capability/strategy/port handler 装配期真 install 进持久 Registry，运行期可解析；
- say() 在 running loop 中走独立线程；输入经 compose.bind_input（ContextVar）按调用链隔离；
- 单次装配（不重复建 store）；say() 只抽 content，不泄漏内部 dict。

compose.Agent 保留为纯装配器（产 Session），本门面是用户默认入口。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Callable, Protocol, cast

from compose import (
    PortCtx, Provider, Registry, ToolSpec, bind_input,
)
from runtime import AgentState, Budgets, CallFn, DynamicValue, Effect, Invoke, Program, Result, Session

from .runtime import _tool_call_handler, assemble_runtime

__all__ = ["Agent"]


class ArchLike(Protocol):
    """架构门面接受的最小协议：能 compile 成 Program。"""
    compile: Callable[[], Program]


def _normalize_history(msgs: list[dict[str, DynamicValue]]) -> list[dict[str, DynamicValue]]:
    """ReAct 内部消息 → OpenAI-compatible（框架内部，用户不碰）。"""
    normalized: list[dict[str, DynamicValue]] = []
    id_by_name: dict[str, str] = {}
    for m in msgs:
        role = str(m.get("role") or "user")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("call_id") or c.get("id") or "call")
                name = str(c.get("name") or "")
                id_by_name[name] = cid
                calls.append({"id": cid, "type": "function", "function": {
                    "name": name,
                    "arguments": str(c.get("arguments") or json.dumps(c.get("args") or {})),
                }})
            normalized.append({"role": "assistant", "content": str(m.get("content") or ""),
                               "tool_calls": calls})
        elif role == "tool":
            name = str(m.get("name") or "")
            normalized.append({"role": "tool",
                               "tool_call_id": str(m.get("call_id") or id_by_name.get(name) or name),
                               "content": json.dumps(m.get("content"), ensure_ascii=False)})
        else:
            normalized.append({"role": role, "content": str(m.get("content") or "")})
    return normalized


_ALLOWED_ANSWER_KEYS = frozenset({"content", "text", "answer", "output"})


def _extract_answer(out: DynamicValue) -> str:
    """从内部产出抽用户文本：白名单字段，任何形态都不泄漏 Effect/Error 信封（Blk S1）。"""
    if isinstance(out, dict):
        for key in _ALLOWED_ANSWER_KEYS:
            if key in out and out[key] is not None:
                return str(out[key])
        return ""  # 无白名单字段：不吐内部 dict
    if isinstance(out, str):
        return out
    return ""  # Result/对象等内部信封：不 str() 泄漏


def _signature_conforms(obj: Provider, proto: type) -> bool:
    """纯签名履约校验（Round2 Blk-1 根因修复）。

    - 不调 isinstance（非 runtime_checkable 协议会抛 TypeError；合法实现被误拒）。
    - 用未绑定方法 getattr(type(obj), name) 对齐协议未绑定方法，避免绑定方法少 self 的错位。
    - 校验：协议的每个必需参数（无默认值、非 self）必须在实现签名中存在且同位置可用。
    - 属性型成员（name 等）：只验存在。
    """
    # 协议成员 = 注解属性 + 类字典中的方法（Protocol 方法不在 __annotations__）
    ann = tuple(getattr(proto, "__annotations__", {}).keys())
    methods = tuple(n for n, v in vars(proto).items() if callable(v) and not n.startswith("_"))
    attrs = tuple(dict.fromkeys(ann + methods))
    for name in attrs:
        expected = getattr(proto, name, None)
        if not callable(expected):
            if not hasattr(obj, name):
                return False
            continue
        actual = getattr(type(obj), name, None)  # 未绑定（含 self）
        if not callable(actual):
            return False
        try:
            sig_e = inspect.signature(expected)
            sig_a = inspect.signature(actual)
        except (TypeError, ValueError):
            continue
        ep = [p for p in sig_e.parameters.values() if p.name not in ("self", "cls")]
        ap = list(sig_a.parameters.values())
        # 实现参数按名字索引（跳过 self/cls）
        a_by_name = {p.name: p for p in ap if p.name not in ("self", "cls")}
        for p in ep:
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            got = a_by_name.get(p.name)
            if got is None:
                return False  # 协议参数名在实现中缺失（Sneaky 反例：register() 无参数）
        # 返回类型多为泛型/前向引用，运行时不做严格类型比对
    return bool(attrs)


class Agent:
    """语义门面：arch + 可注入 capability/策略/工具/端口，say()/run() 直接出结果。

    用户路径（零胶水）：
        Agent(ReAct(tools)).capability(ModelCapability(llm=...)).say("查天气")
    不暴露 Session/Effect/message/ctx；所有装配在执行内部完成。
    """

    name = "agent"
    version = "12.0.0"

    def __init__(self, arch: ArchLike, *, session_id: str = "agent") -> None:
        self._arch = arch
        self._session_id = session_id
        self._caps: dict[str, Provider] = {}
        self._tools: list[tuple[ToolSpec, Callable[..., DynamicValue]]] = []
        self._port_handlers: dict[str, Callable[..., Result]] = {}
        self._registry = Registry()  # 持久注册表：注入的能力 install 到这里，运行期可解析

    def capability(self, cap: Provider) -> "Agent":
        """注入能力（按 name 登记）；装配期真 install 进持久 Registry。"""
        name = str(getattr(cap, "name", "unknown"))
        self._caps[name] = cap
        return self

    def register_tool(self, name: str, fn: Callable[..., DynamicValue], **kwargs: DynamicValue) -> "Agent":
        """注册工具到门面（tool port 走四级路由）。"""
        from compose import DangerLevel
        danger = kwargs.get("danger") or DangerLevel.SAFE
        self._tools.append((
            ToolSpec(name=name, description=str(kwargs.get("description") or ""),
                     danger=danger),
            fn,
        ))
        return self

    def register_port(self, port: str, handler: Callable[..., Result]) -> "Agent":
        """注入自定义 port handler（Blk-2：非 llm/tool port 不再断链）。"""
        self._port_handlers[port] = handler
        return self

    def inject_strategy(self, strategy: Provider) -> "Agent":
        """注入策略：纯签名履约校验（R13 第四要素，Round2 Blk-1）。"""
        from compose.ports import PORT_PROTOCOLS
        matched = [pname for pname, proto in PORT_PROTOCOLS.items()
                   if _signature_conforms(strategy, proto)]
        if not matched:
            raise TypeError(
                f"strategy {type(strategy).__name__} 不满足任何框架协议签名（履约校验失败）")
        self._caps["strategy"] = strategy
        return self

    def _install_caps(self) -> None:
        """装配期真 install 注入的 capability 进持久 Registry（Blk-2）。"""
        for name, cap in self._caps.items():
            if name == "strategy":
                continue
            install = getattr(cap, "install", None)
            if not callable(install):
                raise TypeError(f"capability {name} 无 install()，不满足 Capability 协议")
            install(self, self._registry)

    def _derive_call(self, tool_handler: CallFn) -> CallFn:
        """构造统一 call：llm→dispatch，tool→四级路由，其余→注入的 port handler。"""
        cap = self._caps.get("model")
        dispatch = cast(CallFn | None, getattr(cap, "dispatch", lambda: None)()) if cap else None

        async def call(effect: Effect, ctx: DynamicValue) -> Result:
            if not isinstance(effect, Invoke):
                return Result.failure("INVALID_EFFECT", "facade call only handles Invoke")
            port = effect.port
            if port == "tool":
                return await tool_handler(effect, ctx)
            if port == "llm":
                if dispatch is None:
                    return Result.failure("PORT_MISSING", "no model capability injected")
                return await self._call_llm(effect, ctx, dispatch)
            # 其余 port：查注入的 handler（Blk-2：任意 port 可注入，不再只认 llm/tool）
            handler = self._port_handlers.get(port)
            if handler is not None:
                try:
                    result = handler(effect, ctx)
                    return await result if inspect.isawaitable(result) else result
                except Exception as exc:  # noqa: BLE001
                    return Result.failure("PORT_ERROR", str(exc))
            return Result.failure("PORT_MISSING", f"no handler for port: {port}")

        return cast(CallFn, call)

    @staticmethod
    async def _call_llm(effect: Invoke, ctx: DynamicValue, dispatch: CallFn) -> Result:
        """llm port：ctx 转换 + 消息归一 + ToolCall 归一 + usage 搬运（框架内部）。"""
        pctx = PortCtx(
            session_id=str(getattr(ctx, "session_id", "")),
            run_id=str(getattr(ctx, "run_id", "facade-run")),
            meta=dict(getattr(ctx, "meta", {}) or {}),
        )
        raw = list(effect.payload.get("messages") or [])
        if not raw:
            raw = [{"role": "user", "content": ""}]
        clean = Invoke(port="llm", payload={
            "messages": _normalize_history(raw),
            "model": effect.payload.get("model") or "",
            "tools": effect.payload.get("tools") or [],
        })
        result = await dispatch(clean, pctx)
        data = result.data
        if isinstance(data, dict):
            data = dict(data)
            raw_calls = data.get("tool_calls") or []
            data["tool_calls"] = [
                {"name": tc.name, "args": tc.args, "call_id": tc.call_id,
                 "danger": getattr(tc, "danger", None), "source": getattr(tc, "source", "local")}
                if hasattr(tc, "name") else tc
                for tc in raw_calls
            ]
            usage = data.pop("usage", None)
            if usage is not None and getattr(result, "usage", None) is None:
                return Result(result.ok, data, usage, result.error)
        return Result(result.ok, data, result.usage, result.error)

    async def _arun(self) -> DynamicValue:
        """内部执行：单次装配 → Runner.run → 取 output（全程语义级）。"""
        self._install_caps()
        # 单次装配（Round2 S2）：自建带安全默认的 ToolRegistry，一次 assemble 拿齐 call/store。
        from harness.security import SecurityCapability
        from harness.tools import ToolRegistry
        from .wiring import ApprovingHumanPort, R1AuditEventSink, VerifyingAuditPort

        sec = SecurityCapability()
        registry = ToolRegistry(sandbox=sec.sandbox,
                                human=ApprovingHumanPort(sec.human),
                                audit=VerifyingAuditPort())
        for spec, fn in self._tools:
            registry.register(spec, fn)
        tool_handler = _tool_call_handler(registry)
        call = self._derive_call(tool_handler)
        rt = assemble_runtime(call, stream=None, tools=registry)
        session = Session(
            session_id=self._session_id,
            program_factory=self._arch.compile,
            state=AgentState(budgets=Budgets()),
        )
        result = await rt.runner.run(session)
        return result.output

    async def asay(self, text: str) -> str:
        """异步语义入口：输入经 ContextVar 按调用链绑定（并发安全）。"""
        with bind_input(text):
            return _extract_answer(await self._arun())

    def say(self, text: str) -> str:
        """同步语义入口：running loop 中走独立线程，只返回白名单文本。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.asay(text))
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self.asay(text))).result()

    def run(self, text: str) -> str:
        """语义入口：say 的同义。"""
        return self.say(text)
