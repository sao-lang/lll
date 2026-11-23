"""A4 行为门禁：三级渐进披露在 before_model 相位真触发。

用户注入 DisclosureIndex 到 ToolsCapability → 门面装配 → say 时
disclose() 被真实调用（L1 常驻目录 / L2 命中 schema / L3 技能正文）。
不新增门面 disclosure 入口——符合原设计 R5:295。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import (DangerLevel, DisclosureIndex, DisclosurePacket,
                     PortCtx, ToolSpec)
from harness.model import ModelCapability
from harness.tools import ToolsCapability, tool


class _SpyDisclosure:
    name = "spy_disclosure"
    calls: list[tuple[list[str], list[object]]] = []

    def disclose(self, names: list[str], skills: list[object]) -> DisclosurePacket:
        self.calls.append((names, skills))
        return DisclosurePacket(l1=(), l2=(), l3={}, token_cost=0)


class _LLM:
    name = "fake"
    last_usage = None
    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")
    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@tool(name="gate_tool", description="门禁披露工具", danger=DangerLevel.SAFE)
def _t(x: str = "v") -> str:
    """SAFE 工具。"""
    return x


def test_gate_a4_disclose_triggered_in_before_model() -> None:
    spy = _SpyDisclosure()
    agent = Agent(ReAct([_t], llm="fake"), session_id="gate-a4")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(ToolsCapability(disclosure=spy))  # type: ignore[arg-type]
    agent.register_tool("gate_tool", _t, danger=DangerLevel.SAFE)

    agent.say("用 gate_tool")

    assert spy.calls, "注入的 DisclosureIndex 从未被生产链调用"
