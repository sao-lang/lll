"""RED：agent.stream 公开可观测属性（体检 B10）。

当前只有私有 _stream 和 agent.runtime.stream；用户目标写法要直接 agent.stream。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability
from harness.observability import InMemoryEventStream


def test_red_agent_stream_public_property() -> None:
    """agent.stream 应暴露事件流底座（当前 FAIL：无此属性）。"""
    stream = InMemoryEventStream()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), stream=stream)
    assert agent.stream is stream, "agent.stream 应返回注入的事件流底座"


class _CaptureLLM:
    """最小 LLM 替身：履约 LLMPortWithUsage 签名，返回固定文本。"""
    name = "capture"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_agent_stream_lazy_receives_run_events() -> None:
    """未注入 stream 时，懒建的 agent.stream 必须收到 say() 运行期事件。

    裂缝：say() 装配时 self._stream 仍为 None，assemble_runtime 自建实例存进
    rt.stream；之后访问 agent.stream 才懒建一个新实例——两个实例不同源，
    事件永远收不到（"看起来有 stream，实际是空的"死注入变体）。
    """
    import asyncio

    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=_CaptureLLM()))
    agent.say("你好")

    async def _collect() -> list[object]:
        return [e async for e in agent.stream.subscribe("trace")]

    events = asyncio.run(_collect())
    assert len(events) >= 1, (
        f"agent.stream 懒建实例应与 runtime 事件流同源，实得 {len(events)} 条事件"
    )
