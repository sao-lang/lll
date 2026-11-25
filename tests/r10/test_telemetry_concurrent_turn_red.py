"""RED：并发 asay 下每个 turn span 各自记录真实 duration。

裂缝：_turn_id/_turn_start/_turn_no 是 Trace 实例属性，并发 gather 共享；
_close_turn 按共享 self._turn_id 回写，全部落到最后一个 turn，其余 7 个
duration=0。修后：每个 turn 独立持有 id/start/no，各自写真实 duration。
"""
from __future__ import annotations

import asyncio

import pytest

from api import Agent
from archs import ReAct
from compose import LLMResponse, Usage
from harness.model import ModelCapability
from harness.tools import clear_pending


class _SleepyLLM:
    """每次 complete 真耗时 ~10ms，放大并发交错窗口，保证 duration 可度量。"""
    name = "fake"

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        await asyncio.sleep(0.01)
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1),
                           model=self.name)

    async def stream(self, req: object, ctx: object) -> object:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolated_pending_tools() -> None:
    clear_pending()


def test_concurrent_turns_each_record_real_duration() -> None:
    """gather×8 后 8 个 turn 各自 duration 非零，不共享/不覆盖。"""
    async def run() -> Agent:
        agent = Agent(ReAct([], llm="fake"))
        agent.use(ModelCapability(llm=_SleepyLLM()))
        await asyncio.gather(*[agent.asay(f"g{i}") for i in range(8)])
        return agent

    agent = asyncio.run(run())
    turns = [s for s in agent.trace.spans() if s.name.startswith("turn:")]
    assert len(turns) == 8, f"应有 8 个 turn，实际 {[s.name for s in turns]}"
    zero = [s.name for s in turns if s.duration_ms == 0.0]
    assert not zero, (
        f"并发 turn 的 duration 不得为 0（被共享实例属性覆盖），实际为 0 的 turn={zero}"
    )
    assert all(s.duration_ms > 0.0 for s in turns), (
        f"8 个 turn 各自 duration 必须非零，实际 {[s.duration_ms for s in turns]}"
    )
