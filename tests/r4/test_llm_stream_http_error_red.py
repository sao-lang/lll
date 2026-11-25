"""RED：stream() 的 HTTP 错误必须上抛可识别的原始错误，不得吞成 usage 缺失错误。

独立审计 EXP2-D 证据：后端返回 500，适配器吞掉 HTTPStatusError，网关最终抛
"finished without setting last_usage"，原始状态码与响应体全部丢失。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from compose import LLMRequest, PortCtx
from harness.model import ModelGateway
from harness.model.cache import ExactCache
from ports.adapters.llm import OpenAICompatLLM

REQ = LLMRequest(messages=[{"role": "user", "content": "hi"}])
CTX = PortCtx(session_id="s", run_id="r")


def test_red_stream_http_error_surfaces_original_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    llm = OpenAICompatLLM(base_url="http://m", api_key="SECRET", model="gpt-x",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    cache = ExactCache()
    gateway = ModelGateway(llm=llm, cache=cache)

    async def run() -> None:
        async for _chunk in gateway.stream(REQ, CTX):
            pass

    with pytest.raises(httpx.HTTPStatusError) as caught:
        asyncio.run(run())
    assert caught.value.response.status_code == 500
    assert "finished without setting last_usage" not in str(caught.value)

    cached = asyncio.run(cache.get(REQ))
    assert cached is None, "HTTP 错误不得写缓存"
    assert gateway.ledger.total() == 0.0, "HTTP 错误不得结算成本"
