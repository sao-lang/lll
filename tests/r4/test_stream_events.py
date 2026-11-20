"""R4 §4.1 结构化流式事件测试：TEXT_DELTA / USAGE / DONE + 文本视图兼容。"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, PortCtx, StreamEvent, StreamEventKind, Usage

from harness.model import ModelGateway
from harness.model.stream import wrap_text_stream
from tests.r4.conftest import pctx, req
from tests.r4.fakes import EventStreamLLM


async def _collect(it) -> list:
    return [item async for item in it]


class TestStreamEvents:
    def test_event_llm_emits_structured_events(self) -> None:
        llm = EventStreamLLM(events=[
            StreamEvent(StreamEventKind.TEXT_DELTA, "he"),
            StreamEvent(StreamEventKind.TOOL_CALL_START, {"name": "search", "call_id": "c1"}),
            StreamEvent(StreamEventKind.TEXT_DELTA, "llo"),
            StreamEvent(StreamEventKind.USAGE, Usage(prompt_tokens=5, completion_tokens=2)),
            StreamEvent(StreamEventKind.DONE),
        ])
        events = asyncio.run(_collect(llm.stream_events(req(), pctx())))
        assert [e.kind for e in events] == [
            StreamEventKind.TEXT_DELTA, StreamEventKind.TOOL_CALL_START,
            StreamEventKind.TEXT_DELTA, StreamEventKind.USAGE, StreamEventKind.DONE,
        ]

    def test_stream_is_text_view_of_stream_events(self) -> None:
        """stream() 是 stream_events() 的纯文本视图，向后兼容。"""
        llm = EventStreamLLM(events=[
            StreamEvent(StreamEventKind.TEXT_DELTA, "he"),
            StreamEvent(StreamEventKind.TOOL_CALL_START, {"name": "t", "call_id": "c"}),
            StreamEvent(StreamEventKind.TEXT_DELTA, "llo"),
        ])
        chunks = asyncio.run(_collect(llm.stream(req(), pctx())))
        assert chunks == ["he", "llo"]  # 工具事件被过滤

    def test_gateway_stream_events_settles_usage(self) -> None:
        llm = EventStreamLLM(events=[
            StreamEvent(StreamEventKind.TEXT_DELTA, "hi"),
            StreamEvent(StreamEventKind.USAGE, Usage(prompt_tokens=3, completion_tokens=1, cost_usd=0.2)),
        ])
        gw = ModelGateway(llm=llm)
        asyncio.run(_collect(gw.stream_events(req(), pctx(run_id="r-ev"))))
        assert gw.ledger.run_cost("r-ev") == 0.2

    def test_wrap_text_stream_for_legacy_backend(self) -> None:
        """wrap_text_stream 把旧后端的 stream() 文本包成 TEXT_DELTA（向后兼容）。"""
        class TextOnlyLLM:
            name = "text_only"
            last_usage = Usage()

            async def complete(self, r, c):
                from compose import LLMResponse
                return LLMResponse(content="x")

            def stream(self, r, c):
                async def gen():
                    yield "a"
                    yield "b"
                return gen()

        llm = TextOnlyLLM()
        events = asyncio.run(_collect(wrap_text_stream(llm, req(), pctx())))
        assert [e.kind for e in events] == [
            StreamEventKind.TEXT_DELTA, StreamEventKind.TEXT_DELTA,
            StreamEventKind.USAGE, StreamEventKind.DONE,
        ]
        assert [e.data for e in events[:2]] == ["a", "b"]
