"""回归保护：并发 say_stream 不得串台/丢增量。

背景：怀疑 say_stream 的模块级 ContextVar _stream_queue 在 async generator +
asyncio.gather 并发消费下会上下文交错。用合规 LLM 替身（带 name/usage）实测
三个并发 say_stream 各自拿到自己的完整答复，未复现串台——故本测试是**回归
保护**（首次运行即绿），不是 bug 修复的 RED 证据。若未来 ContextVar 传递方式
变化导致串台，本测试会红。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api import Agent
from archs import ReAct
from compose import LLMResponse, ToolSpec, Usage
from harness.model import ModelCapability


class _TagLLM:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.name = tag
        self.last_usage = None

    async def complete(self, req, ctx) -> LLMResponse:
        await asyncio.sleep(0.01)
        return LLMResponse(content=f"{self.tag}-DONE", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1))

    async def stream(self, req, ctx):
        raise NotImplementedError


def test_red_concurrent_say_stream_isolated() -> None:
    async def run() -> None:
        agents: list[Agent] = []
        for tag in ("A", "B", "C"):
            a = Agent(ReAct([ToolSpec(name="t")], llm="fake"), session_id=tag)
            a.use(ModelCapability(llm=_TagLLM(tag)))
            agents.append(a)

        async def drain(a: Agent) -> str:
            return "".join([chunk async for chunk in a.say_stream("hi")])

        results = await asyncio.gather(*(drain(a) for a in agents))
        assert results == ["A-DONE", "B-DONE", "C-DONE"], \
            f"并发 say_stream 应各自拿到自己的完整答复，实际 {results}"

    asyncio.run(run())
