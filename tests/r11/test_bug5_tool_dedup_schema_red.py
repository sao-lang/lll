"""RED-Bug5: 工具去重按语义（properties/required）判定，不按 len(str(schema))。

审计 EXP7-B：含 properties 的 schema 会输给「description 很长但无 properties」
的空 schema——字符串长度比较选错。修后同名只留语义完整者。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import LLMResponse, ToolSpec, Usage
from harness.model import ModelCapability


class _CaptureLLM:
    name = "capture"

    def __init__(self) -> None:
        self.seen: list[ToolSpec] = []

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        self.seen = list(getattr(req, "tools_catalog", None) or [])
        return LLMResponse(content="OK", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.001), model="fake")

    async def stream(self, req: object, ctx: object) -> object:
        raise NotImplementedError


def test_red_dedup_prefers_properties_over_long_empty_description() -> None:
    """长 description 的空 schema 不得挤掉带 properties 的真 schema。"""
    rich = ToolSpec(name="t", description="rich", parameters_schema={
        "type": "object", "properties": {"x": {"type": "integer"}},
        "required": ["x"]})
    long_empty = ToolSpec(name="t", description="long", parameters_schema={
        "type": "object", "description": "x" * 200})
    assert len(str(long_empty.parameters_schema)) > len(str(rich.parameters_schema))

    agent = Agent(ReAct([], llm="fake"), session_id="bug5-dedup")
    llm = _CaptureLLM()
    agent.use(ModelCapability(llm=llm))
    agent._disclosed_tools = [rich, long_empty]  # 语义完整者先出现

    answer = agent.say("pick a tool")

    assert answer == "OK", f"实得 {answer!r}"
    assert len(llm.seen) == 1, f"同名须去重成 1 项，实得 {len(llm.seen)}"
    got = llm.seen[0]
    assert "properties" in got.parameters_schema, (
        f"必须保留带 properties 的 schema，实得 {got.parameters_schema}")
    assert "required" in got.parameters_schema, (
        f"必须保留带 required 的 schema，实得 {got.parameters_schema}")
    assert got is rich, "应保留语义完整的那一个 spec，不是长 description 空壳"
