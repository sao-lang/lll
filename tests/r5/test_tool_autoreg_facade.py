"""@tool 一份声明三处消费：schema 到 LLM + 工具自动注册执行（体检 B8 闭环）。"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, LLMRequest, LLMResponse, PortCtx, ToolCall, Usage
from harness.model import ModelCapability
from harness.tools import clear_pending, tool


class _ToolLLM:
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.n = 0
        self.schemas: list[dict] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.n += 1
        if req.tools_catalog:
            self.schemas.append(req.tools_catalog[0].parameters_schema)
        if self.n == 1:
            return LLMResponse(content="", tool_calls=[
                ToolCall(call_id="c1", name="get_weather", args={"city": "深圳"})],
                usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")
        return LLMResponse(content="深圳 28°C", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                           model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_tool_declaration_alone_executes_and_reaches_llm() -> None:
    """@tool 单独声明即可：schema 到 LLM + 工具自动执行 + 审计，无需 register_tool。"""
    clear_pending()

    @tool(description="查询天气", danger=DangerLevel.LOW)
    def get_weather(city: str) -> dict:  # type: ignore[no-untyped-def]
        """查询天气"""
        return {"city": city, "temp": 28}

    llm = _ToolLLM()
    agent = Agent(ReAct([get_weather]), system="中文助手").use(ModelCapability(llm=llm))

    answer = agent.say("天气")

    assert answer == "深圳 28°C"
    assert llm.schemas and llm.schemas[0]["properties"]["city"]["type"] == "string"
    assert len(agent.runtime.audit.events()) == 1, "LOW 工具应写审计"
