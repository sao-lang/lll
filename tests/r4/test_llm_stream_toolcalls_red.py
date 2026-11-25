"""RED：流式 delta.tool_calls 分片必须聚合产出 TOOL_CALL_* 事件并重建完整调用。

独立审计 EXP2-B 证据：3 片 index=0 的 tool_calls 分片经 gateway.stream_events
后 TOOL_CALL_* 事件数 = 0（期望 >= 1），工具调用被完整丢弃。
"""
from __future__ import annotations

import asyncio
import json

import httpx

from compose import LLMRequest, PortCtx, StreamEventKind
from harness.model import ModelGateway
from harness.model.cache import ExactCache
from ports.adapters.llm import OpenAICompatLLM

REQ = LLMRequest(messages=[{"role": "user", "content": "weather in Beijing?"}])
CTX = PortCtx(session_id="s", run_id="r")


def _line(obj: object) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _chunk(tool_calls: object = None, content: str | None = None,
           usage: dict[str, float] | None = None) -> dict[str, object]:
    delta: dict[str, object] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    chunk: dict[str, object] = {
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def test_red_stream_tool_call_fragments_become_events() -> None:
    body = b"".join([
        _line(_chunk(tool_calls=[{
            "index": 0, "id": "call_9", "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        }])),
        _line(_chunk(tool_calls=[{
            "index": 0, "function": {"arguments": '{"city":'},
        }])),
        _line(_chunk(tool_calls=[{
            "index": 0, "function": {"arguments": '"Beijing"}'},
        }])),
        _line(_chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.5})),
        b"data: [DONE]\n\n",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    llm = OpenAICompatLLM(base_url="http://m", api_key="SECRET", model="gpt-x",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gateway = ModelGateway(llm=llm, cache=ExactCache())

    async def run() -> list[object]:
        return [e async for e in gateway.stream_events(REQ, CTX)]

    events = asyncio.run(run())
    starts = [e.data for e in events if e.kind is StreamEventKind.TOOL_CALL_START]
    deltas = [e.data for e in events if e.kind is StreamEventKind.TOOL_CALL_DELTA]
    ends = [e.data for e in events if e.kind is StreamEventKind.TOOL_CALL_END]

    assert len(starts) == 1, f"3 片 tool_calls 应聚合出 1 个 START，实际 {len(starts)}：{events!r}"
    assert starts[0] == {"index": 0, "call_id": "call_9", "name": "get_weather"}
    assert len(deltas) == 2, f"两片 arguments 应各发 1 个 DELTA，实际 {len(deltas)}：{events!r}"
    assert "".join(str(d.get("arguments_delta", "")) for d in deltas) == '{"city":"Beijing"}'
    assert len(ends) == 1, f"聚合完成应恰好发 1 个 END，实际 {len(ends)}：{events!r}"
    assert ends[0] == {
        "index": 0, "call_id": "call_9", "name": "get_weather",
        "arguments": {"city": "Beijing"},
    }, f"END 必须给出可执行的完整工具调用，实际 {ends[0]!r}"
    assert gateway.ledger.total() == 0.5, "native 事件路径仍须按 USAGE 正确结算"
