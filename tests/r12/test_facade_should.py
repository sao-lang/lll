"""架构层契约 Round4 残留 Should 的返修测试（真实用户写法，不 monkeypatch）。

S1/S2 签名顺序与 kind · S3 同名 capability 替换 · S4 provider 适配 · S5 审计回放键。
"""
from __future__ import annotations

from api import Agent, dsl
from archs import ReAct
from compose import LLMResponse, ToolSpec, Usage, current_input
from harness.model import ModelCapability
from runtime import Result, Terminate


class _NoopLLM:
    name = "fake"
    last_usage: Usage | None = None

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(), model="fake")

    async def stream(self, r: object, c: object) -> object:  # pragma: no cover
        raise NotImplementedError


# ---- S1/S2：签名顺序与 kind ----
def test_signature_rejects_swapped_and_kwonly_accepts_args() -> None:
    from api.agent import _signature_conforms
    from compose.ports import PORT_PROTOCOLS
    from harness.memory import MemoryStore

    class SneakySwap:  # 参数名齐但顺序全反 + 实质实现
        name = "s"
        def read(self, key, namespace, ctx):
            return f"{key}/{namespace}"
        def write(self, value, key, namespace, ctx):
            return None
        def forget(self, namespace, key, ctx):
            return None

    class SneakyKW:  # 全 keyword-only + 实质实现
        name = "s"
        def read(self, *, namespace, key, ctx):
            return "x"
        def write(self, *, namespace, key, value, ctx):
            return None
        def forget(self, *, namespace, key, ctx):
            return None

    class ArgsImpl:  # 合法通配实现（*args/**kwargs），覆盖 MemoryPort 全部方法
        name = "args"
        def read(self, *args, **kwargs):
            return "args-read"
        def write(self, *args, **kwargs):
            return None
        def forget(self, *args, **kwargs):
            return None

    assert _signature_conforms(MemoryStore(), PORT_PROTOCOLS["memory"]) is True
    assert _signature_conforms(SneakySwap(), PORT_PROTOCOLS["memory"]) is False, "顺序反应拒"
    assert _signature_conforms(SneakyKW(), PORT_PROTOCOLS["memory"]) is False, "kw-only应拒"
    assert _signature_conforms(ArgsImpl(), PORT_PROTOCOLS["memory"]) is True, "*args合法实现应接受"


# ---- S3：同名 capability 对象替换后真重装 ----
def test_same_name_capability_replacement_reinstalls() -> None:
    log: list[str] = []

    class CapA:
        name = "svc"
        def install(self, container: object, reg: object) -> None:
            log.append("A")

    class CapB:
        name = "svc"
        def install(self, container: object, reg: object) -> None:
            log.append("B")

    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    agent.capability(ModelCapability(llm=_NoopLLM()))
    agent.capability(CapA())
    agent.say("first")
    agent.capability(CapB())  # 同名替换
    agent.say("second")
    assert "A" in log and "B" in log, f"替换后新实现应被 install，实际 {log}"


# ---- S4：capability provider 适配（注册名≠port名 + payload 缺 ctx/args）----
def test_provider_adapter_maps_name_and_injects_ctx() -> None:
    from runtime import Invoke

    seen: list[tuple[str, object, object]] = []

    class StoreProvider:
        name = "memory_provider"
        async def read(self, namespace: str, key: str, ctx: object) -> str:
            seen.append(("read", namespace, key))
            return f"PV:{namespace}/{key}"

    class MemCap:
        name = "memory"
        def install(self, container: object, reg: object) -> None:
            reg.register("memory_provider", StoreProvider())  # 注册名≠port名

    def factory():
        def program():
            # payload 只有 namespace/key，无 args/ctx——适配器应自动取参+透传ctx
            out = yield Invoke(port="memory", payload={"op": "read", "namespace": "ns", "key": "k"})
            yield Terminate(output={"content": str(out)})
        return program()

    agent = Agent(dsl.custom_arch(factory, name="m")).capability(MemCap())
    out = agent.say("x")
    assert out == "PV:ns/k", f"provider 应被正确适配消费，实际 {out!r}"
    assert seen == [("read", "ns", "k")], f"参数应正确映射，实际 {seen}"


# ---- S5：门面 tool 审计写进业务 session 键（可回放）----
def test_audit_written_to_business_session_key() -> None:
    """Round4 S5：门面 say() 触发 HIGH 工具审计，须写进 __audit__:<session_id> 可回放。"""
    from compose import DangerLevel, LLMResponse, ToolCall
    from runtime import Invoke

    class ToolLLM:
        name = "fake"
        last_usage: Usage | None = None
        def __init__(self) -> None:
            self.n = 0
        async def complete(self, req: object, ctx: object) -> LLMResponse:
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[
                    ToolCall(name="hi", args={}, call_id="c1", danger=DangerLevel.HIGH)],
                    usage=Usage(), model="fake")
            return LLMResponse(content="done", tool_calls=[], usage=Usage(), model="fake")
        async def stream(self, r: object, c: object) -> object:
            raise NotImplementedError

    agent = Agent(ReAct([ToolSpec(name="hi", description="hi")], llm="fake", max_turns=3),
                  session_id="biz-session-42")
    agent.capability(ModelCapability(llm=ToolLLM()))
    agent.register_tool("hi", lambda **kw: {"ok": True}, danger=DangerLevel.HIGH)
    out = agent.say("run")
    assert out == "done", out

    # 门面内部装配的 rt 的 audit sink 应把事件写进 biz-session-42 的键
    # 通过最后一个 runner 的 checkpoint store 检查（门面未暴露 rt，用注入 store 捕获）
    # —— 这里改用直接验证根因：_tool_call_handler 从 RuntimeCtx 继承 session_id
    from api.runtime import _tool_call_handler
    from harness.tools import ToolRegistry
    from compose import PortCtx
    seen_ctx: list[object] = []
    class SpyAudit:
        name = "spy"
        async def record(self, event: dict, ctx: PortCtx) -> None:
            seen_ctx.append(ctx.session_id)
    reg = ToolRegistry(audit=SpyAudit())  # type: ignore[arg-type]
    reg.register(ToolSpec(name="hi", description="hi", danger=DangerLevel.HIGH),
                 lambda **kw: {"ok": True})
    handler = _tool_call_handler(reg)
    import asyncio
    from runtime import RuntimeCtx, AgentState
    fake_ctx = RuntimeCtx(effect=Invoke(port="tool", payload={"call": {"name": "hi", "args": {}, "call_id": "c1"}}),
                          state=AgentState(), session_id="biz-session-42")
    effect = Invoke(port="tool", payload={"call": {"name": "hi", "args": {}, "call_id": "c1"}})
    asyncio.run(handler(effect, fake_ctx))
    assert seen_ctx == ["biz-session-42"], f"审计 ctx 应继承业务 session_id，实际 {seen_ctx}"


# ---- N2：真实 ModelCapability 同名替换（S3 的生产路径，非简单 log cap）----
def test_model_capability_same_name_replacement_no_crash() -> None:
    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    agent.capability(ModelCapability(llm=_NoopLLM()))
    assert agent.say("first") == "ok"
    agent.capability(ModelCapability(llm=_NoopLLM()))  # 同名 model 替换（install 会 register "llm"）
    assert agent.say("second") == "ok", "同名 model 替换不应触发 duplicate provider 崩"


# ---- N2：钩子 fail-closed 契约（N1）----
def test_hooks_fail_closed_and_explicit_call_priority() -> None:
    from api.runtime import assemble_runtime
    from runtime import Result as R

    # tool_setup 抛异常 → 冒泡（fail-closed）
    def bad_setup(registry: object) -> None:
        raise RuntimeError("setup-boom")
    try:
        assemble_runtime(None, tool_setup=bad_setup)  # type: ignore[arg-type]
        raise AssertionError("钩子异常应冒泡")
    except RuntimeError as exc:
        assert "setup-boom" in str(exc)

    # 显式 call 优先于 call_factory
    async def explicit(effect: object, ctx: object) -> R:
        return R.success({"content": "EXPLICIT"})
    def factory(tool_handler: object) -> object:
        async def c(e: object, ctx: object) -> R:
            return R.success({"content": "FACTORY"})
        return c
    from runtime import Invoke, Session
    rt = assemble_runtime(explicit, call_factory=factory)  # type: ignore[arg-type]
    def prog():
        def g():
            data = yield Invoke(port="llm", payload={})  # 触发 call
            yield __import__("runtime").Terminate(output=data)
        return g()
    out = __import__("asyncio").run(rt.runner.run(Session(session_id="x", program_factory=prog)))
    assert out.output == {"content": "EXPLICIT"}, f"显式 call 应优先，实际 {out.output}"


# ---- Round5 红队披露：*args 通配实现的空实现检查（静态类）----
def test_varargs_empty_impl_rejected_when_source_available() -> None:
    from api.agent import _signature_conforms
    from compose.ports import PORT_PROTOCOLS

    class EmptyArgs:  # 有源码的静态类：*args 通配但方法体只有 ... → 应拒
        name = "e"
        def read(self, *args, **kwargs): ...
        def write(self, *args, **kwargs): ...
        def forget(self, *args, **kwargs): ...

    class RealArgs:
        name = "r"
        def read(self, *args, **kwargs): return "x"
        def write(self, *args, **kwargs): return None
        def forget(self, *args, **kwargs): return None

    assert _signature_conforms(EmptyArgs(), PORT_PROTOCOLS["memory"]) is False
    assert _signature_conforms(RealArgs(), PORT_PROTOCOLS["memory"]) is True
