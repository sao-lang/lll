"""RED：SSE 中途断流必须作为失败上抛，不得把截断内容当成功结算并写缓存。

独立审计 EXP2-C 证据：断流前收到 usage=1.5 后连接 reset，适配器吞异常，
网关把截断的 'Hello truncated' 结算成本 1.5 并写入缓存。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from compose import LLMRequest, PortCtx
from harness.model import ModelGateway
from harness.model.cache import ExactCache
from ports.adapters.llm import OpenAICompatLLM

REQ = LLMRequest(messages=[{"role": "user", "content": "hi"}])
CTX = PortCtx(session_id="s", run_id="r")


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        chunk1 = {"choices": [{"delta": {"content": "Hello"}}]}
        chunk2 = {
            "choices": [{"delta": {"content": " truncated"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost_usd": 1.5},
        }
        yield b"data: " + json.dumps(chunk1).encode() + b"\n\n"
        yield b"data: " + json.dumps(chunk2).encode() + b"\n\n"
        raise httpx.ReadError("connection reset mid-stream")

    async def aclose(self) -> None:
        pass


def test_red_sse_break_is_failure_not_cached_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BrokenStream(),
                              headers={"content-type": "text/event-stream"})

    llm = OpenAICompatLLM(base_url="http://m", api_key="SECRET", model="gpt-x",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    cache = ExactCache()
    gateway = ModelGateway(llm=llm, cache=cache)

    async def run() -> None:
        async for _chunk in gateway.stream(REQ, CTX):
            pass

    with pytest.raises(httpx.ReadError, match="connection reset mid-stream"):
        asyncio.run(run())

    cached = asyncio.run(cache.get(REQ))
    assert cached is None, f"断流不得写缓存，实际缓存了 {cached!r}"
    assert gateway.ledger.total() == 0.0, "断流不得结算任何成本"
    assert gateway.ledger.count == 0, "断流不得记账"
