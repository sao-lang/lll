"""RED：HTTP transport 并发首调必须只握手一次，与 stdio 语义一致。

裂缝：stdio 有 _rpc_lock 串行化含握手的整段 _roundtrip，HTTP 没有，并发首调
各自发 initialize；server 拒绝第二次时一调用失败。

附带判断：initialize 是带 id 的 JSON-RPC 请求，202/204 空体只适用于通知；
server 若用空体"接受"初始化，没有可匹配响应也没有协商结果，必须 fail-closed，
不得按通知语义放行。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ports.adapters.mcp import HttpMCPTransport


def test_red_http_concurrent_first_call_handshakes_once() -> None:
    async def run() -> None:
        state = {"init": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body.get("method")
            if method == "initialize":
                state["init"] += 1
                seq = state["init"]
                await asyncio.sleep(0.01)  # 强制交错，暴露无锁竞态
                if seq >= 2:
                    return httpx.Response(200, json={
                        "jsonrpc": "2.0", "id": body["id"],
                        "error": {"code": -32000, "message": "already initialized"},
                    })
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"], "result": {},
                })
            if method == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"content": [{"type": "text", "text": f"R-{body['id']}"}]},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = HttpMCPTransport("http://x", client=client)
        try:
            results = await asyncio.gather(
                transport.call_tool("srv", "a", {}),
                transport.call_tool("srv", "b", {}),
                return_exceptions=True,
            )
            assert state["init"] == 1, f"initialize 应恰好 1 次，实际 {state['init']}"
            assert results == ["R-2", "R-3"], f"两次调用都应拿到自己 id 的响应，实际 {results}"
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("status", [202, 204])
def test_red_http_initialize_empty_body_is_rejected(status: int) -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = HttpMCPTransport("http://x", client=client)
        try:
            with pytest.raises(RuntimeError, match="no JSON-RPC response"):
                await transport.call_tool("srv", "a", {})
        finally:
            await client.aclose()

    asyncio.run(run())
