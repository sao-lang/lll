"""RED：并发流共享 self.last_usage 导致 A 被记成 B 的 usage（成本串台）。

独立审计 EXP2-B 证据：A 先写 3.0 后暂停，B 覆盖为 7.0 并完成，A 恢复后
网关读到 7.0，账本合计 14.0 而非 3.0 + 7.0 = 10.0。
"""
from __future__ import annotations

import asyncio
import json

import httpx

from compose import LLMRequest, PortCtx
from harness.model import ModelGateway
from harness.model.cache import ExactCache
from ports.adapters.llm import OpenAICompatLLM

CTX = PortCtx(session_id="s", run_id="r")
REQ_A = LLMRequest(messages=[{"role": "user", "content": "QUERY-A"}])
REQ_B = LLMRequest(messages=[{"role": "user", "content": "QUERY-B"}])


def _line(obj: object) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


class _StreamA(httpx.AsyncByteStream):
    def __init__(self, pause: asyncio.Event, release: asyncio.Event) -> None:
        self.pause = pause
        self.release = release

    async def __aiter__(self):
        yield _line({"choices": [{"delta": {"content": "A"}}]})
        yield _line({"choices": [], "usage": {"cost_usd": 3.0}})
        self.pause.set()
        await self.release.wait()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        pass


class _StreamB(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield _line({"choices": [{"delta": {"content": "B"}}]})
        yield _line({"choices": [], "usage": {"cost_usd": 7.0}})
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        pass


def test_red_concurrent_streams_usage_is_isolated() -> None:
    async def run() -> tuple[dict[str, float], float]:
        pause, release = asyncio.Event(), asyncio.Event()
        seen: dict[str, float] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "QUERY-A" in request.content.decode():
                return httpx.Response(200, stream=_StreamA(pause, release),
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, stream=_StreamB(),
                                  headers={"content-type": "text/event-stream"})

        llm = OpenAICompatLLM(base_url="http://m", api_key="SECRET", model="gpt-x",
                              client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        gateway = ModelGateway(llm=llm, cache=ExactCache())

        async def drain(request: LLMRequest, tag: str) -> list[str]:
            chunks = [c async for c in gateway.stream(request, CTX)]
            usage = llm.last_usage
            assert usage is not None, f"{tag} 流末须有 usage"
            seen[tag] = usage.cost_usd  # 各任务读自己的 last_usage，串台时 A 会读到 7.0
            return chunks

        task_a = asyncio.create_task(drain(REQ_A, "a"))
        await asyncio.wait_for(pause.wait(), 2)
        out_b = await drain(REQ_B, "b")
        release.set()
        out_a = await task_a
        assert out_a == ["A"] and out_b == ["B"]
        return seen, gateway.ledger.total()

    seen, total = asyncio.run(run())
    assert seen == {"a": 3.0, "b": 7.0}, f"并发流各自读到自己的 usage，实际 {seen}"
    assert total == 10.0, f"A/B 两流合计应为 3.0 + 7.0 = 10.0，实际 {total}"
