"""R4-C7 流式编排测试：逐块转发 / 缓存命中 / 取消可中断 / 成本结算。

按 R4 文档 §4.1 的 ModelGateway.stream() 契约（v1.1）。
"""
from __future__ import annotations

import asyncio

from hooks import LLMRequest, LLMResponse, PortCtx, Usage

from harness.model import ModelGateway, StructuredOutputSpec
from tests.r4.conftest import pctx, req, sample_schema
from tests.r4.fakes import RecordingLLM, StreamLLM


async def _collect(it) -> list[str]:
    """收集异步迭代器全部元素。"""
    return [item async for item in it]


class TestGatewayStream:
    def test_stream_forwards_chunks(self) -> None:
        """逐块转发：后端 stream 的每块原样产出。"""
        llm = StreamLLM(chunks=["he", "llo"])
        gw = ModelGateway(llm=llm)
        out = asyncio.run(_collect(gw.stream(req(), pctx())))
        assert out == ["he", "llo"]

    def test_cache_hit_yields_cached_content(self) -> None:
        """缓存命中：逐块吐缓存内容，不调后端 stream。"""
        llm = RecordingLLM([LLMResponse(content="cached", usage=Usage())])
        gw = ModelGateway(llm=llm)
        asyncio.run(_collect(gw.stream(req(), pctx())))  # 首次：进缓存
        before = llm.call_count
        out = asyncio.run(_collect(gw.stream(req(), pctx())))  # 命中
        assert "".join(out) == "cached"
        assert llm.call_count == before

    def test_cancel_is_interruptible(self) -> None:
        """取消：消费一块后 aclose()，不阻塞、不产出剩余块。"""
        llm = StreamLLM(chunks=["a", "b", "c"])
        gw = ModelGateway(llm=llm)

        async def consume_one():
            it = gw.stream(req(), pctx())
            first = await anext(it)
            await it.aclose()
            return first

        assert asyncio.run(consume_one()) == "a"

    def test_stream_cost_recorded(self) -> None:
        """正常结束按 usage 结算成本。"""
        llm = RecordingLLM([LLMResponse(content="hi", usage=Usage(cost_usd=0.25))])
        gw = ModelGateway(llm=llm)
        asyncio.run(_collect(gw.stream(req(), pctx(run_id="r-stream"))))
        assert gw.ledger.run_cost("r-stream") == 0.25

    def test_stream_ignores_structured_spec(self) -> None:
        """流式下结构化校验禁用：即便 spec 不合法也不抛（§4.1 第 6 条）。"""
        llm = RecordingLLM([LLMResponse(content="not json", usage=Usage())])
        gw = ModelGateway(llm=llm, spec=StructuredOutputSpec(schema=sample_schema()))
        out = asyncio.run(_collect(gw.stream(req(), pctx())))
        assert "".join(out) == "not json"

    def test_stream_without_usage_refuses(self) -> None:
        """后端未写 last_usage → 显式拒答（不记假零成本）。"""
        class _NoUsageLLM:
            """故意不写 last_usage 的后端。"""
            name = "no_usage_llm"
            last_usage: Usage | None = None

            async def complete(self, r, c) -> LLMResponse:
                return LLMResponse(content="x")

            async def stream(self, r, c):
                yield "x"

        gw = ModelGateway(llm=_NoUsageLLM())  # type: ignore[arg-type]

        async def consume():
            it = gw.stream(req(), pctx())
            try:
                [item async for item in it]
            except ValueError as exc:
                assert "last_usage" in str(exc)
                return "refused"
            return "allowed"

        assert asyncio.run(consume()) == "refused"
