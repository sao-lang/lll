"""RED：wrap_text_stream 必须在流末读取最新 last_usage 并发 USAGE 事件。

独立审计 EXP6-B 证据：fake 后端在流中才写 last_usage，包装器在生成器创建时
快照为 None → USAGE 事件数 0（期望 1）。
"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, LLMResponse, PortCtx, StreamEventKind, Usage
from harness.model.stream import wrap_text_stream


class _LateUsageLLM:
    name = "late-usage"

    def __init__(self) -> None:
        self.last_usage: Usage | None = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:  # pragma: no cover
        return LLMResponse()

    def stream(self, req: LLMRequest, ctx: PortCtx):
        async def gen():
            self.last_usage = Usage(cost_usd=2.0)  # 真实适配器：流中才写入
            yield "hi"
        return gen()


def test_red_wrap_text_stream_emits_usage_from_latest_last_usage() -> None:
    async def run() -> None:
        llm = _LateUsageLLM()
        events = [e async for e in wrap_text_stream(llm, LLMRequest(), PortCtx())]

        usages = [e for e in events if e.kind is StreamEventKind.USAGE]
        assert len(usages) == 1, f"必须恰好发 1 个 USAGE 事件，实际 {len(usages)}：{events!r}"
        assert isinstance(usages[0].data, Usage)
        assert usages[0].data.cost_usd == 2.0
        assert [e.kind for e in events] == [
            StreamEventKind.TEXT_DELTA, StreamEventKind.USAGE, StreamEventKind.DONE
        ]

    asyncio.run(run())
