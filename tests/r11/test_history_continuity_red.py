"""RED：跨 say 对话连续性（门面自动维护并重放会话历史）。

裂缝：每次 say() 重新装配新 Session，第 2 次 say 的 LLM 只收到本轮 user 消息，
完全看不到上一轮——门面自诩"聊天语义入口"却无对话连续性（探针实证）。
长期记忆（MemoryCapability）是提炼写入，不是逐轮对话上下文，两者正交。
"""
from __future__ import annotations

import pytest

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability


class _RecordingLLM:
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.turns: list[list[dict]] = []

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        self.turns.append([dict(m) for m in req.messages])
        return LLMResponse(content=f"回复{len(self.turns)}", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_second_say_sees_first_round_history() -> None:
    """第 2 次 say 的 LLM 必须收到第 1 轮的问答对（框架自动重放）。"""
    llm = _RecordingLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))

    agent.say("我叫小明")
    agent.say("我叫什么？")

    assert len(llm.turns) == 2
    first_round = str(llm.turns[1])
    assert "小明" in first_round, f"第 2 轮未收到第 1 轮历史：{first_round}"
    assert "回复1" in first_round, f"第 2 轮未收到第 1 轮回答：{first_round}"


def test_red_history_property_and_clear() -> None:
    """agent.history 只读可查；clear 后下一轮不再携带历史。"""
    llm = _RecordingLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))

    agent.say("我叫小明")
    assert len(agent.history) == 2, "history 应含 user+assistant 两条"

    agent.clear_history()
    assert len(agent.history) == 0
    agent.say("我叫什么？")
    assert "小明" not in str(llm.turns[-1]), "clear 后不应再携带历史"
