"""A3 行为门禁：MCP tools 按原设计注册进门面执行链的同一 ToolRegistry。

用户注入 MCP transport 到 ToolsCapability → Agent 装配 → MCPManager.connect()
把远端 tools（mcp__server__name）注册进 wired_tools → 执行链可见可调用。
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import ReAct
from compose import DangerLevel, PortCtx, ToolCall, ToolSpec
from harness.model import ModelCapability
from harness.tools import MCPManager, MCPTransport, ToolsCapability


class _FakeMCP(MCPTransport):
    name = "fake_mcp"
    calls: list[tuple[str, str, dict]] = []

    async def list_tools(self, server: str) -> list[ToolSpec]:
        return [ToolSpec(name="remote_echo", description="远端回显",
                         danger=DangerLevel.LOW,
                         parameters_schema={"type": "object",
                                            "properties": {"x": {"type": "string"}}})]

    async def call_tool(self, server: str, tool_name: str,
                        args: dict) -> object:
        self.calls.append((server, tool_name, args))
        return {"echo": args.get("x")}

    async def ping(self, server: str) -> bool:
        return True


class _LLM:
    name = "fake"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_gate_a3_mcp_tools_reach_facade_execution_chain() -> None:
    transport = _FakeMCP()
    agent = Agent(ReAct([], llm="fake"), session_id="gate-a3")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(ToolsCapability(mcp=transport, mcp_servers=("gate_a3",)))  # type: ignore[arg-type]

    asyncio.run(agent._arun())
    rt = agent.runtime
    assert rt is not None and rt.tools is not None

    names = [s.name for s in rt.tools.catalog(level=1)]
    assert "mcp__gate_a3__remote_echo" in names, f"MCP tools 未进执行链：{names}"

    result = asyncio.run(rt.tools.call(
        ToolCall(call_id="c1", name="mcp__gate_a3__remote_echo",
                 args={"x": "hello"}),
        PortCtx(session_id="gate-a3")))
    assert result.ok, f"MCP 工具调用失败：{result.error}"
    assert transport.calls and transport.calls[0][2] == {"x": "hello"}
