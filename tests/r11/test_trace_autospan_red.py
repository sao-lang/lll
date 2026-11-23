"""RED：门面 Trace span 自动埋点（llm/tool 调用自动产 span）。

裂缝：Runner 不自动产 span，Trace 埋点全靠用户手动——门面"自动编排"承诺
缺可观测最后一格。修后：say() 期间 llm/tool 调用自动包 span（装配层 call
wrapper，不改 R1），agent.trace 只读可查。
"""
from __future__ import annotations

import pytest

from api import Agent
from archs import ReAct
from compose import DangerLevel, ToolSpec
from harness.model import ModelCapability
from harness.observability import NoopTelemetry
from harness.tools import tool, clear_pending


@pytest.fixture(autouse=True)
def _isolated_pending_tools() -> None:
    clear_pending()


class _ToolLLM:
    name = "fake"
    last_usage = None
    _called = False

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, ToolCall, Usage
        if not self._called:
            self._called = True
            return LLMResponse(
                content="", tool_calls=[ToolCall(call_id="c1", name="get_weather",
                                                 args={"city": "深圳"})],
                usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                model="fake")
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=2, completion_tokens=2,
                                       cost_usd=0.0002), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@tool(name="get_weather", description="查天气", danger=DangerLevel.SAFE)
def _weather(city: str = "深圳") -> dict[str, object]:
    """本地确定性天气。"""
    return {"city": city, "temperature": 28}


def test_red_facade_auto_spans_for_llm_and_tool() -> None:
    """say() 后 agent.trace 应有 llm/tool 自动 span（duration>0）。"""
    agent = Agent(ReAct([_weather], llm="fake"))
    agent.use(ModelCapability(llm=_ToolLLM()))

    agent.say("查深圳天气")

    spans = agent.trace.spans()
    components = {s.component for s in spans}
    assert "llm" in components, f"缺 llm span，实得 {components}"
    assert "tool" in components, f"缺 tool span，实得 {components}"
    assert all(s.duration_ms >= 0 for s in spans)
    assert all(s.trace_id == agent.trace.trace_id for s in spans)


def test_red_agent_trace_is_shared_across_says() -> None:
    """同一 agent 多次 say 共用同一 trace（trace_id 稳定）。"""
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=_ToolLLM()))
    agent.say("一次")
    trace_id = agent.trace.trace_id
    agent.say("两次")
    assert agent.trace.trace_id == trace_id
    assert len(agent.trace.spans()) >= 2
