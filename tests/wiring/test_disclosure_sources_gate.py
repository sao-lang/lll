"""Blocker2 RED：披露必须同源执行链且产出真进 LLM tools_catalog。

- 注入的 DisclosureIndex 必须看到执行链 rt.tools 的工具（不是能力包自己的空 registry）
- 披露产出 L1/L2 必须进入 LLM 调用的 tools payload（不是被丢弃）
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import ReAct
from compose import (DangerLevel, DisclosureIndex, DisclosurePacket,
                     PortCtx, ToolCall, ToolSpec)
from harness.model import ModelCapability
from harness.tools import ToolsCapability, tool


@tool(name="gate_disclose_tool", description="披露验证工具", danger=DangerLevel.SAFE)
def _t(x: str = "v") -> str:
    """SAFE 工具。"""
    return x


class _RecordingLLM:
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.tools_seen: list[list[object]] = []
        self._n = 0

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        self.tools_seen.append(list(req.tools_catalog or []))
        self._n += 1
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _SpyDisclosure:
    name = "spy_disclosure"
    packets: list[DisclosurePacket] = []
    seen_names: list[list[str]] = []

    def disclose(self, names: list[str], skills: list[object]) -> DisclosurePacket:
        self.seen_names.append(names)
        specs = tuple(ToolSpec(name=n, description="d") for n in names)
        packet = DisclosurePacket(l1=specs, l2=(), l3={}, token_cost=0)
        self.packets.append(packet)
        return packet


def test_blocker2_disclosure_uses_execution_registry_and_reaches_llm() -> None:
    spy = _SpyDisclosure()
    llm = _RecordingLLM()
    agent = Agent(ReAct([], llm="fake"), session_id="gate-b2-disclose")
    agent.use(ModelCapability(llm=llm))
    agent.use(ToolsCapability(disclosure=spy))  # type: ignore[arg-type]
    agent.register_tool("gate_disclose_tool", _t, danger=DangerLevel.SAFE)

    agent.say("用工具")

    assert spy.seen_names and "gate_disclose_tool" in spy.seen_names[0], (
        f"披露索引看不到执行链工具（不同源）：{spy.seen_names}")
    assert any(llm.tools_seen), "披露产出未进入 LLM tools_catalog"
