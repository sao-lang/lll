"""用户自定义 MCP 客户端接线测试：证明按 MCPTransport 接口实现即可被框架消费。

这不是 fake 替身测试——它模拟"用户写一个框架未内置的客户端"的完整路径：
实现接口 → 履约校验 → 注入 MCPManager → connect 加前缀注册 → registry.call 端到端。
"""
from __future__ import annotations

import asyncio

from compose import DangerLevel, PortCtx, make_call
from harness.tools import MCPManager, MCPTransport, ToolRegistry


class MyMCPClient:
    """按《自定义MCP客户端.md》模板写的用户客户端（无继承，结构化子类型）。"""

    name = "my_mcp"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def list_tools(self, server: str) -> list[object]:
        """返回原始工具名（不加 mcp__ 前缀）。"""
        from compose import ToolSpec
        return [ToolSpec(
            name="search", description="搜索",
            parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            danger=DangerLevel.SAFE, source="mcp",
        )]

    async def call_tool(self, server: str, tool: str, args: dict[str, object]) -> object:
        """记录调用并返回结果（模拟用户协议）。"""
        self.calls.append((server, tool, args))
        return f"{server}:{tool}:{args.get('q', '')}"

    async def ping(self, server: str) -> bool:
        return server == "my-server"


def test_user_client_satisfies_interface() -> None:
    """履约校验：用户客户端被 isinstance(MCPTransport) 接受。"""
    assert isinstance(MyMCPClient(), MCPTransport)


def test_user_client_end_to_end() -> None:
    """接线：用户客户端注入 MCPManager → connect 加前缀 → registry.call 无差别消费。"""
    ctx = PortCtx(session_id="s", run_id="r")
    client = MyMCPClient()
    registry = ToolRegistry()
    mcp = MCPManager(registry, client)

    specs = asyncio.run(mcp.connect("my-server", ctx))
    assert specs[0].name == "mcp__my-server__search"       # 框架加前缀
    assert specs[0].name in [s.name for s in registry.catalog()]  # 进同一 Registry

    result = asyncio.run(registry.call(
        make_call("mcp__my-server__search", {"q": "hello"}), ctx,
    ))
    assert result.ok and result.value == "my-server:search:hello"
    assert result.meta["source"] == "mcp"
    assert client.calls == [("my-server", "search", {"q": "hello"})]  # 真被用户客户端处理


def test_user_client_ping_via_health() -> None:
    """health 走用户客户端的 ping（框架门面委托到用户实现）。"""
    ctx = PortCtx()
    mcp = MCPManager(ToolRegistry(), MyMCPClient())
    asyncio.run(mcp.connect("my-server", ctx))
    asyncio.run(mcp.connect("other", ctx))
    assert asyncio.run(mcp.health(ctx)) == {"my-server": True, "other": False}
