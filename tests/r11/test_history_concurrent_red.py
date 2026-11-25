"""RED：并发 asay 每个 run 都必须注入历史，不得因共享 _history_pending 丢失。

裂缝：_history_pending 是实例级布尔，第一个 LLM 调用置 False 后，其余并发 run
跳过注入 → 5/6 次调用只剩本轮输入（无历史），但 history 仍被追加看似正常。
修后：按 messages 形态判定「本轮首轮」（只有 user、无 assistant/tool），
不依赖任何跨 run 共享可变标志。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability


class _CaptureLLM:
    name = "capture"
    last_usage = None
    calls: list = []

    async def complete(self, req, ctx):
        type(self).calls.append([dict(m) for m in req.messages])
        from compose import LLMResponse, Usage
        return LLMResponse(content="OK", tool_calls=[], usage=Usage(), model=self.name)

    async def stream(self, req, ctx):
        async def gen(): yield "OK"
        return gen()


def test_red_concurrent_asay_injects_history_every_run() -> None:
    _CaptureLLM.calls = []
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    llm = _CaptureLLM()
    agent.use(ModelCapability(llm=llm))
    agent.say("seed")  # 建立历史

    async def run() -> None:
        await asyncio.gather(*[agent.asay(f"w{i}") for i in range(6)])
    asyncio.run(run())

    # seed 之后有 6 次并发调用，每个的首轮 LLM messages 都应含 "seed"
    after_seed = _CaptureLLM.calls[1:]
    assert len(after_seed) == 6, f"应有 6 次并发调用，实际 {len(after_seed)}"
    with_seed = [msgs for msgs in after_seed
                 if any(m.get("content") == "seed" for m in msgs)]
    assert len(with_seed) == 6, \
        f"每次并发调用都应注入历史 seed，实际只有 {len(with_seed)}/6 次"
