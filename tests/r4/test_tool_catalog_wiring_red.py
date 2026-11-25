"""RED：@tool 推导的 schema 必须真正到达 LLMRequest.tools_catalog（体检 B8 三链断裂）。

链路：@tool → ToolSpec(schema) → ReAct payload["tools"] → gateway._center → LLMRequest.tools_catalog。
当前 gateway 丢弃 payload["tools"]，adapter 因 tools_catalog=None 不发 tools 给 LLM。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, LLMRequest, LLMResponse, PortCtx, ToolSpec, Usage
from harness.model import ModelCapability
from harness.tools import clear_pending, tool


@tool(description="查询天气", danger=DangerLevel.LOW)
def get_weather(city: str) -> dict:  # type: ignore[no-untyped-def]
    """docstring 描述"""
    return {"city": city}


# @tool 装饰生成 get_weather.spec（本用例要验证的 schema 来源），但本用例不执行
# 工具、装配期不会消费 pending；若不清空会泄漏到后续 e2e 用例造成 duplicate tool。
clear_pending()


class _CaptureLLM:
    name = "capture"
    last_usage = None
    seen_catalog: list = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.seen_catalog.append(req.tools_catalog)
        return LLMResponse(content="done", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                           model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_tool_schema_reaches_llm() -> None:
    """schema 必须到 LLMRequest.tools_catalog（当前 FAIL：gateway 丢弃）。"""
    llm = _CaptureLLM()
    arch = ReAct([get_weather.spec], llm="fake")
    agent = Agent(arch).capability(ModelCapability(llm=llm))

    agent.say("查天气")

    assert llm.seen_catalog, "LLM 应被调用"
    catalog = llm.seen_catalog[0]
    assert catalog, f"tools_catalog 应含工具，实得 {catalog!r}"
    assert catalog[0].parameters_schema.get("properties", {}).get("city", {}).get("type") == "string", (
        f"city: str 的 schema 应到 LLM，实得 {catalog[0].parameters_schema!r}"
    )
