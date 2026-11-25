"""RED：无 USAGE 帧的流不得结算成另一并发流的 usage（成本串台）。

裂缝：last_usage getter 在 _usage_var.get() 为 None 时回退实例级
_last_usage_compat；并发流 A 写入 compat=A 的账，B 流开头 self.last_usage=None
只在 B 的 ContextVar 写 None，getter 仍回退到 compat → B 拿到 A 的 cost，
gateway 把 B 也结算 3.0（total 6.0，应为 3.0）。
修后：用哨兵区分"本上下文未设置"与"显式设为 None"；仅未设置时才回退 compat。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import httpx
from compose import LLMRequest, PortCtx
from harness.model.cache import ExactCache
from harness.model.gateway import ModelGateway
from ports.adapters.llm import OpenAICompatLLM

CTX = PortCtx(session_id="s", run_id="r")


def _line(obj: object) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def test_red_no_usage_stream_does_not_inherit_other_task_usage() -> None:
    a_content, b_content, resume_a, resume_b = (asyncio.Event() for _ in range(4))

    class StreamA(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _line({"choices": [{"index": 0, "delta": {"content": "A"}}]})
            a_content.set()
            await resume_a.wait()
            yield _line({"choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 3.0}})
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            pass

    class StreamB(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _line({"choices": [{"index": 0, "delta": {"content": "B"}}]})
            b_content.set()
            await resume_b.wait()
            yield b"data: [DONE]\n\n"  # 全程无 usage

        async def aclose(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        if b"QUERY-A" in request.content:
            return httpx.Response(200, stream=StreamA(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, stream=StreamB(),
                              headers={"content-type": "text/event-stream"})

    async def run() -> None:
        llm = OpenAICompatLLM(
            base_url="http://m", api_key="K", model="m",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        gw = ModelGateway(llm=llm, cache=ExactCache())
        req_a = LLMRequest(messages=[{"role": "user", "content": "QUERY-A"}])
        req_b = LLMRequest(messages=[{"role": "user", "content": "QUERY-B"}])

        async def drain(it):
            return [x async for x in it]

        # A、B 都启动并卡在首块之后；此时两者都显式写了 last_usage=None。
        task_a = asyncio.create_task(drain(gw.stream(req_a, CTX)))
        await asyncio.wait_for(a_content.wait(), 2)
        task_b = asyncio.create_task(drain(gw.stream(req_b, CTX)))
        await asyncio.wait_for(b_content.wait(), 2)

        # A 先写入 usage（compat 被 A 污染），B 后结束且全程无 usage。
        resume_a.set()
        await asyncio.wait_for(task_a, 2)
        resume_b.set()
        try:
            await asyncio.wait_for(task_b, 2)
        except ValueError:
            pass  # 正确行为：B 无 usage 应拒绝结算，不记假成本

        assert gw.ledger.total() == 3.0, \
            f"仅 A 有 usage，账本应为 3.0，实际 {gw.ledger.total()}（B 串了 A 的账）"
        assert gw.ledger.count == 1, \
            f"仅 A 应结算一次，实际 count={gw.ledger.count}"

    asyncio.run(run())
