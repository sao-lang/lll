"""RED：stdio 在响应前收到合法通知帧时不得毒化单管道。

裂缝：_handshake/_roundtrip 只 recv 一次；server 先推无 id 的 notifications/*
帧时 result_of_matched 因 id=None 抛错，真正的响应滞留队列，后续请求永久错位。

修后边界：
- 跳过：无 id 键 + method 为 notifications/* 的合法通知帧；
- fail-closed：带 id 的帧（含 id=null）、无 id 但 method 非通知、非 dict 帧、
  以及超过跳过上限的通知洪流；跳过帧累计有上限，防恶意 server 死等。
"""
from __future__ import annotations

import asyncio

import pytest

from ports.adapters.mcp import StdioChannel, StdioMCPTransport

# 跳过上限的期望值（实现须与之对齐；RED 阶段实现尚无该常量）。
MAX_SKIP = 64


class ScriptedChannel(StdioChannel):
    """按脚本发帧：可在 initialize/tools/call 响应前插入任意前置帧。"""

    name = "scripted"

    def __init__(self, preamble: list[dict] | None = None,
                 call_preamble: list[dict] | None = None) -> None:
        self.queue: list[dict] = []
        self.recved: list[dict] = []
        self.preamble = list(preamble or [])
        self.call_preamble = list(call_preamble or [])

    async def send(self, message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            self.queue.extend(self.preamble)
            self.queue.append({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        elif method == "tools/call":
            self.queue.extend(self.call_preamble)
            self.queue.append({
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"content": [{"type": "text", "text": f"R-{message['id']}"}]},
            })

    async def recv(self) -> dict:
        frame = self.queue.pop(0)
        self.recved.append(frame)
        return frame

    async def close(self) -> None:
        pass


def test_red_notification_before_initialize_response_is_skipped() -> None:
    async def run() -> None:
        ch = ScriptedChannel(preamble=[{
            "jsonrpc": "2.0", "method": "notifications/message", "params": {"x": 1},
        }])
        transport = StdioMCPTransport(ch)
        assert await transport.call_tool("srv", "a", {}) == "R-2"
        assert await transport.call_tool("srv", "b", {}) == "R-3"
        # initialize 通知被跳过一次；id=1/2/3 三个响应各自正确配对，无残留。
        assert [frame.get("method") for frame in ch.recved] == [
            "notifications/message", None, None, None,
        ]
        assert [frame.get("id") for frame in ch.recved] == [None, 1, 2, 3]
        assert ch.queue == []

    asyncio.run(run())


def test_red_notification_before_regular_response_is_skipped() -> None:
    async def run() -> None:
        ch = ScriptedChannel(call_preamble=[{
            "jsonrpc": "2.0", "method": "notifications/tools/list_changed",
        }])
        transport = StdioMCPTransport(ch)
        assert await transport.call_tool("srv", "a", {}) == "R-2"
        # 握手响应 + 通知 + tools/call 响应，通知不得被当成 id 匹配失败。
        assert [frame.get("method") for frame in ch.recved] == [
            None, "notifications/tools/list_changed", None,
        ]
        assert [frame.get("id") for frame in ch.recved] == [1, None, 2]

    asyncio.run(run())


def test_red_non_notification_frame_without_id_fails_closed() -> None:
    async def run() -> None:
        ch = ScriptedChannel(preamble=[{"jsonrpc": "2.0", "method": "server/hello"}])
        transport = StdioMCPTransport(ch)
        with pytest.raises(RuntimeError, match="id mismatch"):
            await transport.call_tool("srv", "a", {})
        # 非通知形态的帧绝不被跳过：只读了一帧，真正的 initialize 响应仍滞留。
        assert len(ch.recved) == 1
        assert ch.queue == [{"jsonrpc": "2.0", "id": 1, "result": {}}]

    asyncio.run(run())


def test_red_notification_flood_is_bounded() -> None:
    async def run() -> None:
        ch = ScriptedChannel(preamble=[{
            "jsonrpc": "2.0", "method": "notifications/message",
        }] * (MAX_SKIP + 1))
        transport = StdioMCPTransport(ch)
        with pytest.raises(RuntimeError, match="notification flood"):
            await transport.call_tool("srv", "a", {})
        # 上限精确：多出的那一帧触发拒绝，不再继续吞掉真正的响应。
        assert len(ch.recved) == MAX_SKIP + 1
        assert ch.queue == [{"jsonrpc": "2.0", "id": 1, "result": {}}]

    asyncio.run(run())
