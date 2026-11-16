"""M4-T04 MCP 工具注入：命名空间、同一 Registry、只注入 L1、红线。"""
from __future__ import annotations

import pytest

from harness.tools import ToolRegistry
from harness.tools.fakes import FakeMCPTransport, FakeSandbox, StaticApprover
from harness.tools.mcp import MCPManager
from ports import DangerLevel, PortCtx, ToolCall, ToolSpec
from tests.fakes import run


def registry():
    reg = ToolRegistry(sandbox=FakeSandbox(), approver=StaticApprover())
    reg.register(ToolSpec(name="local_echo", description="本地工具",
                          parameters_schema={"type": "object", "properties": {}},
                          danger=DangerLevel.SAFE), lambda args, c: "local")
    return reg


def test_t04_connect_injects_namespaced_tools_as_l1_only():
    reg = registry()
    manager = MCPManager(reg, FakeMCPTransport())
    specs = run(manager.connect("demo"))
    assert [s.name for s in specs] == ["mcp__demo__add", "mcp__demo__weather"]
    catalog = reg.catalog(level=1)
    assert {s.name for s in catalog} >= {"local_echo", "mcp__demo__add"}
    assert all(s.parameters_schema == {} for s in catalog)  # MCP 未全量注入
    assert all(s.source == "mcp" for s in specs)
    assert reg.schema_of("mcp__demo__add")["properties"]["a"]["type"] == "integer"


def test_t04_mcp_and_local_share_one_registry():
    reg = registry()
    manager = MCPManager(reg, FakeMCPTransport())
    run(manager.connect("demo"))
    remote = run(manager.call("mcp__demo__add", {"a": 2, "b": 5}, PortCtx("s")))
    local = run(reg.call(ToolCall(call_id="l1", name="local_echo", args={}), PortCtx("s")))
    assert remote.ok and remote.value == 7 and remote.meta["source"] == "mcp"
    assert local.value == "local"
    assert run(manager.health()) == {"demo": True}


def test_t04_unknown_transport_tool_and_unhealthy_server():
    class Broken(FakeMCPTransport):
        async def ping(self, server):
            return False

        async def call_tool(self, server, tool, args, ctx):
            raise ValueError("unknown")

    reg = registry()
    manager = MCPManager(reg, Broken())
    run(manager.connect("demo"))
    assert run(manager.health()) == {"demo": False}
    out = run(manager.call("mcp__demo__add", {}, PortCtx("s")))
    assert not out.ok and "unknown" in out.error
    missing = run(manager.call("mcp__demo__nope", {}, PortCtx("s")))
    assert not missing.ok and "unknown tool" in missing.error


def test_t04_full_injection_red_line_is_rejected():
    leak = ToolRegistry(index_enabled=False, sandbox=FakeSandbox(), approver=StaticApprover())
    run(MCPManager(leak, FakeMCPTransport()).connect("demo"))
    assert any("MCP tools fully injected" in e for e in leak.static_check())
