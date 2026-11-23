"""RED：用户目标 API —— use() 链式插拔 + @tool + system + say + stream。

用户目标写法（体检主线）：
    agent = Agent(ReAct([get_weather]), system="你是简洁的中文助手")
    agent.use(ModelCapability(llm=llm)).use(MemoryCapability())
    answer = agent.say("深圳天气？")
    agent.stream
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, LLMRequest, LLMResponse, PortCtx, Usage
from harness.model import ModelCapability


class _FakeLLM:
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.n += 1
        return LLMResponse(content="深圳 28°C 晴", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                           model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_use_chained_api() -> None:
    """use() 应链式返回 Agent 且能力真生效（当前 FAIL：无 use）。"""
    agent = Agent(ReAct([], llm="fake"), system="你是简洁的中文助手")
    result = agent.use(ModelCapability(llm=_FakeLLM()))
    assert result is agent, "use() 应链式返回 self"

    answer = agent.say("深圳天气？")
    assert answer == "深圳 28°C 晴"
    assert agent.stream is not None, "agent.stream 一等公开"
