"""FR-4/5/6/11 RED：MCP server 经框架真实 StdioMCPTransport 握手/列表/调用。

- 天气 MCP server：tools/list 含 get_weather；tools/call 返回真实或离线标记。
- 搜索 MCP server：tools/list 含 search；调用返回结构化文本。
- 不造假：离线时返回明确标记而非编造天气/结果。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
SERVERS = ROOT / "examples" / "react-chat" / "mcp_servers"


def _channel(script: str):
    from ports.adapters.mcp import SubprocessStdioChannel
    return SubprocessStdioChannel([sys.executable, str(SERVERS / script)])


def test_red_mcp_weather_server_handshake_and_list() -> None:
    import asyncio
    from ports.adapters.mcp import StdioMCPTransport

    async def _run() -> None:
        ch = _channel("weather_server.py")
        transport = StdioMCPTransport(ch)
        specs = await transport.list_tools("weather")
        names = [s.name for s in specs]
        assert "get_weather" in names, f"天气 MCP 未暴露 get_weather：{names}"
        assert await transport.ping("weather")
        await transport.aclose()
    asyncio.run(_run())


def test_red_mcp_weather_offline_returns_explicit_marker(monkeypatch: object) -> None:
    import asyncio
    from ports.adapters.mcp import StdioMCPTransport
    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]

    async def _run() -> None:
        ch = _channel("weather_server.py")
        transport = StdioMCPTransport(ch)
        await transport.list_tools("weather")
        text = await transport.call_tool("weather", "get_weather", {"city": "深圳"})
        assert "offline" in text.lower(), f"离线未明确标记：{text}"
        await transport.aclose()
    asyncio.run(_run())


def test_red_mcp_search_server_exposes_search() -> None:
    import asyncio
    from ports.adapters.mcp import StdioMCPTransport

    async def _run() -> None:
        ch = _channel("search_server.py")
        transport = StdioMCPTransport(ch)
        specs = await transport.list_tools("search")
        assert "search" in [s.name for s in specs]
        await transport.aclose()
    asyncio.run(_run())


