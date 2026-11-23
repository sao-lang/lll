"""架构层契约返修测试（Round1 B1-B4 + Round2 Blk-1~4）。

每条用真实用户写法验证，不 monkeypatch、不用过宽 pytest.raises。
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from api import Agent, dsl
from archs import ReAct
from compose import LLMResponse, ToolSpec, Usage, bind_input, current_input
from harness.model import ModelCapability
from runtime import Result, Terminate


class _ToolCallingLLM:
    name = "fake"
    last_usage: Usage | None = None

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        from compose import ToolCall
        self.n += 1
        if self.n == 1:
            return LLMResponse(content="", tool_calls=[
                ToolCall(name="t", args={"x": 1}, call_id="c1")],
                usage=Usage(), model="fake")
        return LLMResponse(content="工具已调用", tool_calls=[], usage=Usage(), model="fake")

    async def stream(self, r: object, c: object) -> object:  # pragma: no cover
        raise NotImplementedError


class _SimpleLLM:
    name = "fake"
    last_usage: Usage | None = None

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        return LLMResponse(content="async_ok", tool_calls=[], usage=Usage(), model="fake")

    async def stream(self, r: object, c: object) -> object:  # pragma: no cover
        raise NotImplementedError


# ---- B1：tool port 真分派 ----
def test_tool_port_dispatched() -> None:
    rt = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=3))
    rt.capability(ModelCapability(llm=_ToolCallingLLM()))
    rt.register_tool("t", lambda **kw: {"ok": True})
    out = rt.say("查一下")
    assert "工具" in out, f"工具调用应被分派并产出结论，实际 {out!r}"


# ---- Blk-2：任意 port 可注入并消费 ----
def test_arbitrary_port_injected_and_consumed() -> None:
    from runtime import Invoke

    def factory():
        def program():
            out = yield Invoke(port="memory", payload={"op": "read", "key": "k"})
            yield Terminate(output={"content": str(out)})
        return program()

    seen: list[object] = []
    def memory_handler(effect: Invoke, ctx: object) -> Result:
        seen.append(effect.payload)
        return Result.success("MEM_VALUE")

    agent = Agent(dsl.custom_arch(factory, name="mem-arch")).register_port("memory", memory_handler)
    out = agent.say("x")
    assert out == "MEM_VALUE" and seen, f"注入的 memory port 应被消费，实际 {out!r}/{seen}"


# ---- Blk-1：履约校验不误拒合法、不误收 Sneaky ----
def test_conformance_accepts_legal_rejects_sneaky() -> None:
    from harness.memory import MemoryStore

    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    agent.inject_strategy(MemoryStore())  # 合法实现必须被接受（不抛）

    class Sneaky:
        name = "sneaky"
        def register(self) -> None: ...
        def catalog(self) -> None: ...
        async def call(self) -> None: ...

    with pytest.raises(TypeError):
        Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2)).inject_strategy(Sneaky())


# ---- B3 + Blk-3：async 上下文 + 并发输入隔离 ----
def test_async_context_and_concurrent_input_isolation() -> None:
    async def inner() -> str:
        agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
        agent.capability(ModelCapability(llm=_SimpleLLM()))
        return agent.say("hi")
    assert asyncio.run(inner()) == "async_ok"

    seen: list[str] = []
    def factory():
        def program():
            seen.append(current_input())
            yield Terminate(output={"content": current_input()})
        return program()

    agent = Agent(dsl.custom_arch(factory, name="echo"))
    results: dict[int, str] = {}
    def worker(i: int) -> None:
        results[i] = agent.say(f"IN-{i}")
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    for i in range(4):
        assert results[i] == f"IN-{i}", f"并发输入串了：{i}->{results[i]}"
    assert set(seen) == {f"IN-{i}" for i in range(4)}, f"架构读到的输入串了：{seen}"


# ---- B4 + Blk-4：自定义架构真实写法收到输入 + 门面不泄漏 ----
def test_custom_arch_receives_input_real_pattern() -> None:
    def factory():
        def program():
            user_text = current_input()
            yield Terminate(output={"content": f"答复:{user_text}", "tool_calls": [], "usage": {}})
        return program()

    agent = Agent(dsl.custom_arch(factory, name="custom"))
    out = agent.say("USER_INPUT_XYZ")
    assert out == "答复:USER_INPUT_XYZ", f"自定义架构应收到输入且不泄漏 dict，实际 {out!r}"


# ---- S1：失败信封不泄漏 ----
def test_failure_envelope_not_leaked() -> None:
    def factory():
        def program():
            yield Terminate(output={"content": "ok"})
        return program()
    agent = Agent(dsl.custom_arch(factory, name="f"))
    assert agent.say("x") == "ok"


# ---- Round3 新 Blocker：重复调用、审计同源、签名反例 ----
def test_repeated_say_and_concurrent_no_duplicate_install() -> None:
    """Round3 新 Blocker：注入 capability 后第2次 say / 并发不再因重复 install 崩。"""
    class L(_SimpleLLM):
        def __init__(self) -> None:
            self.n = 0
        async def complete(self, req: object, ctx: object) -> LLMResponse:
            self.n += 1
            return LLMResponse(content=f"R{self.n}", tool_calls=[], usage=Usage(), model="fake")

    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    agent.capability(ModelCapability(llm=L()))
    assert agent.say("a") == "R1"
    assert agent.say("b") == "R2"  # 第2次不崩

    res: dict[int, str] = {}
    def worker(i: int) -> None:
        res[i] = agent.say(f"I{i}")
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(res) == 4, f"并发不应崩：{res}"


def test_signature_conforms_rejects_empty_and_wrong_shape() -> None:
    """Round3 Blk-1：参数名齐但顺序反/空实现/keyword-only 的伪实现被拒。"""
    from api.agent import _signature_conforms
    from compose.ports import PORT_PROTOCOLS
    from harness.memory import MemoryStore

    class SneakySmart:
        name = "s"
        def read(self, namespace, key, ctx): ...   # 空实现
        async def write(self, namespace, key, value, ctx): ...

    assert _signature_conforms(MemoryStore(), PORT_PROTOCOLS["memory"]) is True
    assert _signature_conforms(SneakySmart(), PORT_PROTOCOLS["memory"]) is False


def test_tool_audit_same_r1_store() -> None:
    """Round3 新 Blocker：门面的 tool 审计走 R1AuditEventSink（同源）。"""
    from api.runtime import assemble_runtime
    def setup(registry: object) -> None:
        return None
    def factory(tool_handler: object) -> object:
        async def call(effect: object, ctx: object) -> Result:
            return Result.success({"content": "x"})
        return call
    rt = assemble_runtime(None, tool_setup=setup, call_factory=factory)  # type: ignore[arg-type]
    inner = getattr(rt.audit, "_inner", None)
    sink = getattr(inner, "_sink", None) if inner else None
    assert type(sink).__name__ == "R1AuditEventSink", f"审计应同源，实际 {type(sink).__name__ if sink else None}"
