"""RED：MCP JSON-RPC 响应必须按 id 匹配，不得把并发中别人的响应当自己的。

裂缝：StdioMCPTransport._roundtrip 是 send→recv 一发一收，并发 gather 时多个
caller 各自 recv 抢响应，后发先回即错配。HTTP 也不校验响应 id。
修后：stdio 串行化 in-flight（单管道标准语义）+ 响应 id 与请求 id 必须匹配，
不匹配 fail-closed（RuntimeError），不静默返回错结果。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ports.adapters.mcp import StdioMCPTransport, StdioChannel


class ReorderingChannel(StdioChannel):
    name = "reorder"

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.sent: dict[str, int] = {}

    async def send(self, message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            self.responses.append({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        elif method == "tools/call":
            rid = message["id"]
            self.sent[str(message.get("params", {}).get("name"))] = rid
            result = {"content": [{"type": "text", "text": f"RESULT-FOR-ID-{rid}"}]}
            self.responses.insert(0, {"jsonrpc": "2.0", "id": rid, "result": result})
        elif method == "tools/list":
            self.responses.append({"jsonrpc": "2.0", "id": message["id"],
                                   "result": {"tools": []}})

    async def recv(self) -> dict:
        await asyncio.sleep(0)
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


class MismatchChannel(StdioChannel):
    """server 返回与请求 id 不符的响应（协议违约）。"""
    name = "mismatch"

    def __init__(self) -> None:
        self.step = 0

    async def send(self, message: dict) -> None:
        if message.get("method") == "initialize":
            self.responses = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            self.responses = {"jsonrpc": "2.0", "id": 99999, "result": {"content": []}}

    async def recv(self) -> dict:
        await asyncio.sleep(0)
        return self.responses

    async def close(self) -> None:
        pass


def test_red_concurrent_calls_get_own_response() -> None:
    async def run() -> None:
        ch = ReorderingChannel()
        t = StdioMCPTransport(ch)
        await t.list_tools("srv")
        a, b = await asyncio.gather(
            t.call_tool("srv", "A", {}),
            t.call_tool("srv", "B", {}),
        )
        assert a == f"RESULT-FOR-ID-{ch.sent['A']}", \
            f"A 应得自己 id={ch.sent['A']} 的响应，实际 {a}"
        assert b == f"RESULT-FOR-ID-{ch.sent['B']}", \
            f"B 应得自己 id={ch.sent['B']} 的响应，实际 {b}"

    asyncio.run(run())


def test_red_response_id_mismatch_fails_closed() -> None:
    async def run() -> None:
        t = StdioMCPTransport(MismatchChannel())
        try:
            await t.call_tool("srv", "t", {})
        except RuntimeError as exc:
            assert "id mismatch" in str(exc) or "mismatch" in str(exc), \
                f"应报 id 不匹配错误，实际 {exc}"
        else:
            raise AssertionError("响应 id 不符却静默成功，应 fail-closed")

    asyncio.run(run())
