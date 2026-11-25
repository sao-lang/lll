"""RED：工具目录同名去重必须保留带 JSON Schema 的版本。

裂缝：披露包 L1（schema 空）+ L2（schema 有）同名，当前去重保留先出现的 L1
空 schema → OpenAI-compatible 后端（DeepSeek）不知参数，真网工具调用失败。
修后：同名保留 parameters_schema 更完整者，LLM 真拿到可填参数 schema。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability


class _CaptureLLM:
    name = "capture"
    last_usage = None
    seen_tools: list[object] = []

    async def complete(self, req, ctx):
        type(self).seen_tools = list(req.tools_catalog or [])
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(), model=self.name)

    async def stream(self, req, ctx):
        raise NotImplementedError


def test_red_dedup_keeps_schema_rich_toolspec() -> None:
    """同名 L1(空schema)+L2(有schema) → LLM 实收的 ToolSpec 必须带 schema。"""
    _CaptureLLM.seen_tools = []
    agent = Agent(ReAct([], llm="fake"))
    llm = _CaptureLLM()
    agent.use(ModelCapability(llm=llm))
    # 模拟披露结果：同名两项，L1 schema 空、L2 schema 全
    empty = ToolSpec(name="get_weather", description="L1")
    rich = ToolSpec(name="get_weather", description="L2",
                    parameters_schema={"type": "object",
                                       "properties": {"city": {"type": "string"}},
                                       "required": ["city"]})
    agent._disclosed_tools = [empty, rich]
    agent.say("查天气")
    tools = _CaptureLLM.seen_tools
    assert len(tools) == 1, f"同名应去重成 1 项，实际 {len(tools)}"
    got = tools[0]
    assert "city" in (getattr(got, "parameters_schema", {}) or {}).get("properties", {}), \
        f"LLM 应拿到带 city 参数的 schema，实际 {getattr(got, 'parameters_schema', None)}"
