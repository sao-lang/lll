"""架构层契约返修 RED 测试（独立 review Round1 的 4 个 Blocker）。

B1 tool 断链 / B2 注入假 / B3 async 崩 / B4 自定义架构输入失效+数据泄漏。
每条复现 review 的内联反例，修完必须转绿。
"""
from __future__ import annotations

import asyncio

import pytest

import api
from api import Agent, dsl
from archs import ReAct
from compose import LLMResponse, ToolPort, ToolSpec, Usage
from harness.model import ModelCapability
from runtime import Invoke, Result, Session, Terminate


class _ToolCallingLLM:
    """会真的发起 tool_calls 的 LLM（复现 B1：tool port 断链）。"""
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


# ---- B1：tool port 断链 ----
def test_semantic_facade_dispatches_tool_port() -> None:
    """注入 model 能力后，ReAct 的 tool_calls 必须真走工具，不能 PORT_MISSING。"""
    rt = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=3))
    rt.capability(ModelCapability(llm=_ToolCallingLLM()))
    # 注册工具到门面（新增 API）；当前无此入口 → FAIL
    rt.register_tool("t", lambda **kw: {"ok": True})
    out = rt.say("查一下")
    assert "工具" in out, f"工具调用应被分派并产出结论，实际 {out!r}"


# ---- B2：履约校验 + 注入消费 ----
def test_injection_consumed_and_weak_conformance_rejected() -> None:
    """履约校验要验签名（不是只看成员名）；注入的 capability 必须真 install。"""
    class Sneaky:
        """成员名齐全但签名全错——runtime_checkable isinstance 会误接受。"""
        name = "sneaky"
        def register(self) -> None: ...       # 缺 name 参数
        def catalog(self) -> None: ...        # 缺 level 参数
        async def call(self) -> None: ...     # 缺 call/ctx 参数

    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    with pytest.raises((TypeError, ValueError)):
        agent.inject_strategy(Sneaky())  # 弱履约对象应被拒

    # capability 注入后应真 install（进 Registry）
    class MemCap:
        name = "memory"
        def __init__(self) -> None:
            self.installed = False
        def install(self, container: object, reg: object) -> None:
            self.installed = True
    mem = MemCap()
    agent.capability(mem)
    agent.say("hi")
    assert mem.installed, "注入的 capability 必须被真 install（进运行期消费）"


# ---- B3：async 上下文调用门面崩溃 ----
def test_facade_usable_inside_running_event_loop() -> None:
    """在已有事件循环里（FastAPI/Jupyter）调用门面不能崩。"""
    async def inner() -> str:
        agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
        class SimpleLLM:
            name = "fake"
            last_usage: Usage | None = None
            async def complete(self, req: object, ctx: object) -> LLMResponse:
                return LLMResponse(content="async_ok", tool_calls=[], usage=Usage(), model="fake")
            async def stream(self, r: object, c: object) -> object:
                raise NotImplementedError
        agent.capability(ModelCapability(llm=SimpleLLM()))
        return agent.say("hi")  # 当前 asyncio.run 在 running loop 内 → RuntimeError

    assert asyncio.run(inner()) == "async_ok"


# ---- B4：自定义架构输入注入 + 不泄漏内部数据 ----
def test_custom_arch_receives_input_and_facade_hides_internal_data() -> None:
    """用户自定义架构要收到输入；门面只返回回答文本，不吐 message/effect dict。"""
    seen: dict[str, object] = {}

    holder: dict[str, object] = {}
    def factory() -> object:
        def program() -> object:
            # 自定义架构按契约从门面注入的 input_text 读用户输入
            seen["payload"] = holder.get("input_text")
            yield Terminate(output={"content": "答案", "tool_calls": [], "usage": {}})
        return program()

    custom = dsl.custom_arch(factory, name="custom")
    # 门面 setattr(arch, "input_text", text)；自定义架构通过 holder 镜像该值（模拟真实读取）
    original_compile = custom.compile
    def compile_with_input() -> object:
        holder["input_text"] = getattr(custom, "input_text", None)
        return original_compile()
    custom.compile = compile_with_input  # type: ignore[assignment]
    agent = Agent(custom)
    out = agent.say("USER_INPUT_XYZ")
    assert out == "答案", f"门面应只返回回答文本，不泄漏内部 dict，实际 {out!r}"
    payload = seen.get("payload")
    assert payload and any("USER_INPUT_XYZ" in str(v) for v in (
        payload.values() if isinstance(payload, dict) else [payload])), (
        f"自定义架构应收到用户输入，实际 payload={payload!r}")
