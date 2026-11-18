"""R5-T04：MCP 合并 —— list_tools→fqname 前缀注入同一 Registry，health 走 ping。"""
from __future__ import annotations

import asyncio

from hooks import PortCtx
from harness.tools import MCPManager, MCPTransport, ToolRegistry

from .conftest import FakeTransport

def test_transport_protocol_satisfied() -> None:
    """FakeTransport 履约 MCPTransport（isinstance 校验）。"""
    assert isinstance(FakeTransport(), MCPTransport)


def test_mcp_manager_implements_mcp_port() -> None:
    """MCPManager 实现 R2 MCPPort（带 ctx 的三方法 + name）。"""
    from hooks import MCPPort
    mgr = MCPManager(ToolRegistry(), FakeTransport())
    assert isinstance(mgr, MCPPort)


def test_connect_injects_fqname_into_registry(ctx: PortCtx) -> None:
    """connect 把工具以 mcp__<server>__<tool> 注入同一 Registry。"""
    registry = ToolRegistry()
    mgr = MCPManager(registry, FakeTransport())
    injected = asyncio.run(mgr.connect("srv", ctx))
    assert injected[0].name == "mcp__srv__search"
    assert "mcp__srv__search" in [s.name for s in registry.catalog()]


def test_mcp_call_via_registry(ctx: PortCtx) -> None:
    """MCP 工具经统一 Registry.call 调用，无差别（返回 ToolResult）。"""
    registry = ToolRegistry()
    transport = FakeTransport()
    mgr = MCPManager(registry, transport)
    asyncio.run(mgr.connect("srv", ctx))
    # 用统一 call 入口：名字为 fqname
    from hooks import make_call
    result = asyncio.run(registry.call(make_call("mcp__srv__search", {"q": "x"}), ctx))
    assert result.ok and result.value == "srv/search"
    assert transport.call_log == [("srv", "search")]


def test_health_uses_ping(ctx: PortCtx) -> None:
    """health 对每个 server 调 ping，返回可达性字典。"""
    registry = ToolRegistry()
    mgr = MCPManager(registry, FakeTransport())
    asyncio.run(mgr.connect("alive", ctx))
    asyncio.run(mgr.connect("dead", ctx))
    health = asyncio.run(mgr.health(ctx))
    assert health == {"alive": True, "dead": False}
