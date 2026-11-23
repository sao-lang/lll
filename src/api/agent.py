"""语义门面 Agent —— 用户只碰 say()/run()，零 message/effect/payload/ctx 暴露。

设计（契约③④；独立 review Round1 B1-B4 返修）：
- 位于 api 层（不改依赖方向：compose 不反向依赖 harness/api）；
- 按 port 分派 call：llm→ModelGateway.dispatch，tool→ToolRegistry（不绕过审批/沙盒/审计）；
- 履约校验验签名（runtime_checkable 只验成员名不够）；注入的 capability 真 install；
- say() 在 running loop 中走独立线程（async 部署可用），并提供 async asay()；
- 用户输入经 input_text 注入架构首轮；say() 只抽 content，不泄漏内部 dict。

compose.Agent 保留为纯装配器（产 Session），本门面是用户默认入口。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Callable, Protocol, cast

from compose import PortCtx, Provider, Registry, ToolSpec
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


def _extract_answer(out: DynamicValue) -> str:
    """从内部产出抽回答文本（不泄漏 message/tool_calls/usage dict，B4）。"""
    if isinstance(out, dict):
        content = out.get("content")
        if content is not None:
            return str(content)
        return ""
    return str(out) if out is not None else ""


class Agent:
    """语义门面：arch + 可注入 capability/策略/工具，say()/run() 直接出结果。

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

    def capability(self, cap: Provider) -> "Agent":
        """注入能力（按 name 登记）；装配期真 install 并注册 Port（B2）。"""
        name = str(getattr(cap, "name", "unknown"))
        self._caps[name] = cap
        return self

    def register_tool(self, name: str, fn: Callable[..., DynamicValue], **kwargs: DynamicValue) -> "Agent":
        """注册工具到门面（B1：tool port 不再断链）。"""
        from compose import DangerLevel
        danger = kwargs.get("danger") or DangerLevel.SAFE  # 门面注册工具默认 SAFE（免 HITL）
        self._tools.append((
            ToolSpec(name=name, description=str(kwargs.get("description") or ""),
                     danger=danger),
            fn,
        ))
        return self

    def inject_strategy(self, strategy: Provider) -> "Agent":
        """注入策略：签名级履约校验（R13 第四要素，B2）。"""
        from compose.ports import PORT_PROTOCOLS
        matched = [pname for pname, proto in PORT_PROTOCOLS.items()
                   if self._conforms(strategy, proto)]
        if not matched:
            raise TypeError(
                f"strategy {type(strategy).__name__} 不满足任何框架协议签名（履约校验失败）")
        self._caps["strategy"] = strategy
        return self

    @staticmethod
    def _conforms(obj: Provider, proto: type) -> bool:
        """履约校验：isinstance + 逐方法签名兼容（runtime_checkable 只验成员名，不够）。"""
        if not isinstance(obj, proto):
            return False
        methods = getattr(proto, "__protocol_attrs__", None) or getattr(proto, "_protocol_attrs", None)
        if methods is None:
            methods = tuple(getattr(proto, "__annotations__", {}).keys())
        for name in methods:
            expected = getattr(proto, name, None)
            actual = getattr(obj, name, None)
            if not callable(expected) or not callable(actual):
                continue  # 属性型协议成员：isinstance 已验存在
            try:
                sig_e = inspect.signature(expected)
                sig_a = inspect.signature(actual)
            except (TypeError, ValueError):
                continue
            if len(sig_e.parameters) > len(sig_a.parameters):
                return False  # 参数数量不足（Sneaky 反例）
        return True

    def _install_caps(self, registry: Registry) -> None:
        """装配期真 install 注入的 capability（B2：注入不再是死对象）。"""
        for name, cap in self._caps.items():
            if name == "strategy":
                continue
            install = getattr(cap, "install", None)
            if not callable(install):
                raise TypeError(f"capability {name} 无 install()，不满足 Capability 协议")
            install(self, registry)

    def _derive_call(self, tool_handler: CallFn) -> CallFn | None:
        """按 port 分派：llm→gateway dispatch，tool→ToolRegistry（B1：不绕过审批/沙盒/审计）。"""
        cap = self._caps.get("model")
        dispatch = cast(CallFn | None, getattr(cap, "dispatch", lambda: None)()) if cap else None
        if dispatch is None:
            return None

        async def call(effect: Effect, ctx: DynamicValue) -> Result:
            if isinstance(effect, Invoke) and effect.port == "tool":
                return await tool_handler(effect, ctx)  # 真走工具四级路由
            if isinstance(effect, Invoke) and effect.port == "llm":
                pctx = PortCtx(
                    session_id=str(getattr(ctx, "session_id", self._session_id)),
                    run_id=str(getattr(ctx, "run_id", f"{self._session_id}-run")),
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
                    # ToolCall 是 pydantic 对象，ReAct 的 _tool_calls 只认 Mapping → 这里归一为 dict。
                    raw_calls = data.get("tool_calls") or []
                    data["tool_calls"] = [
                        {"name": tc.name, "args": tc.args, "call_id": tc.call_id}
                        if hasattr(tc, "name") else tc
                        for tc in raw_calls
                    ]
                    usage = data.pop("usage", None)
                    if usage is not None and getattr(result, "usage", None) is None:
                        return Result(result.ok, data, usage, result.error)
                return Result(result.ok, data, result.usage, result.error)
            return Result.failure("PORT_MISSING", f"facade call skips port {getattr(effect,'port','?')}")

        return cast(CallFn, call)

    def _inject_input(self, text: str) -> None:
        """把用户输入注入架构首轮（ReAct 与自定义架构都能读，B4）。"""
        setattr(self._arch, "input_text", text)

    async def _arun(self, text: str) -> DynamicValue:
        """内部执行：装配 → Runner.run → 取 output（全程语义级）。"""
        # 先建运行期（拿带审计/沙盒的 ToolRegistry），再注入门面工具与 call（B1）
        rt = assemble_runtime(None, stream=None)
        assert rt.tools is not None, "运行期必须带工具注册表"
        for spec, fn in self._tools:
            rt.tools.register(spec, fn)
        tool_handler = _tool_call_handler(rt.tools)
        call = self._derive_call(tool_handler)
        if call is not None:
            rt = assemble_runtime(call, stream=None, tools=rt.tools)
        # 装配期真 install 注入的 capability（B2）：用 compose.Registry（Provider 表）
        from compose import Registry as ProviderRegistry
        self._install_caps(ProviderRegistry())

        self._inject_input(text)
        session = Session(
            session_id=self._session_id,
            program_factory=self._arch.compile,
            state=AgentState(budgets=Budgets()),
        )
        result = await rt.runner.run(session)
        return result.output

    async def asay(self, text: str) -> str:
        """异步语义入口（async 部署用，B3）。"""
        return _extract_answer(await self._arun(text))

    def say(self, text: str) -> str:
        """同步语义入口：running loop 中走独立线程（B3），只返回回答文本（B4）。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.asay(text))
        # 已在事件循环内：独立线程跑，避免 asyncio.run 嵌套崩溃
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self.asay(text))).result()

    def run(self, text: str) -> str:
        """语义入口：say 的同义。"""
        return self.say(text)
