"""RED：Agent.say_stream(text) 一等流式 API —— 逐字吐 LLM 文本增量。

设计：与 say/asay 对称的公开方法，返回 AsyncIterator[str]，内部走
ModelCapability.gateway().stream_events() 原生事件，逐 TEXT_DELTA yield。
无 ContextVar 开关、无 topic 转手、无 examples 胶水。
无流式后端时回退 complete：一次性 yield 完整答复（诚实，不伪装流式）。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from api import Agent
from archs import ReAct
from compose import StreamEvent, StreamEventKind, Usage
from harness.model import ModelCapability


class _StreamingLLM:
    name = "streaming"
    last_usage = None

    async def complete(self, req, ctx):
        from compose import LLMResponse
        return LLMResponse(content="fallback", tool_calls=[],
                           usage=Usage(prompt_tokens=5, completion_tokens=3), model=self.name)

    def stream_events(self, req, ctx) -> AsyncIterator[StreamEvent]:
        async def gen():
            for chunk in ("你", "好", "，", "世", "界"):
                yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk)
                await asyncio.sleep(0)
            self.last_usage = Usage(prompt_tokens=5, completion_tokens=3)
            yield StreamEvent(kind=StreamEventKind.USAGE, data=self.last_usage)
            yield StreamEvent(kind=StreamEventKind.DONE)
        return gen()

    async def stream(self, req, ctx):
        async def gen():
            yield "你好，世界"
        return gen()


def test_red_say_stream_yields_text_deltas() -> None:
    agent = Agent(ReAct([], llm="fake"))
    agent.use(ModelCapability(llm=_StreamingLLM()))

    async def scenario() -> None:
        chunks = [c async for c in agent.say_stream("你好")]
        assert chunks == ["你", "好", "，", "世", "界"], f"应逐块吐增量，实际 {chunks}"

    asyncio.run(scenario())


def test_red_say_stream_falls_back_to_complete() -> None:
    """无流式后端（只有 complete）时一次性吐完整答复，不伪装逐字。"""
    class _CompleteOnly:
        name = "complete-only"
        last_usage = None
        async def complete(self, req, ctx):
            from compose import LLMResponse
            return LLMResponse(content="一次性答复", tool_calls=[],
                               usage=Usage(prompt_tokens=1, completion_tokens=2), model=self.name)
        def stream(self, req, ctx):
            async def gen(): yield "一次性答复"
            return gen()

    agent = Agent(ReAct([], llm="fake"))
    agent.use(ModelCapability(llm=_CompleteOnly()))

    async def scenario() -> None:
        chunks = [c async for c in agent.say_stream("你好")]
        assert chunks == ["一次性答复"], f"无流式后端应一次性吐，实际 {chunks}"

    asyncio.run(scenario())
