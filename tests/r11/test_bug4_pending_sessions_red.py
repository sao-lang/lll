"""RED-Bug4: 挂起期间的第二次 say 不能静默覆盖首个挂起会话。

审计 P9c：say1 挂起 → say2 再挂起 → 单槽 _pending_session 只留 say2，
say1 永远丢。本框架选 fail-closed：有未恢复挂起会话时拒绝新 say（显式报错），
resume 仍精确续跑首个会话。
"""
from __future__ import annotations

import pytest

from api import Agent
from compose import LLMResponse, Usage, current_input
from harness.model import ModelCapability
from runtime import Invoke, Program, Suspend, Terminate


class _InputSensitiveArch:
    name = "input_sensitive"

    def __init__(self) -> None:
        self.compile_inputs: list[str] = []

    def compile(self) -> Program:
        inp = current_input()
        self.compile_inputs.append(inp)

        def program():
            yield Invoke(port="llm", payload={
                "messages": [{"role": "user", "content": inp}]})
            yield Suspend(question="approve?", options=["y", "n"])
            yield Terminate(output=f"inp={inp!r}")
            return None

        return program()


class _EchoLLM:
    name = "echo"

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        return LLMResponse(content="FIRST", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.001), model="fake")

    async def stream(self, req: object, ctx: object) -> object:
        raise NotImplementedError


def test_red_second_say_while_suspended_is_rejected() -> None:
    """say1 挂起后 say2 必须报错，不能静默把 say1 挤出唯一槽位。"""
    arch = _InputSensitiveArch()
    agent = Agent(arch, session_id="bug4-pending")  # type: ignore[arg-type]
    agent.use(ModelCapability(llm=_EchoLLM()))

    first = agent.say("first-input")
    assert "wait_human" in first, f"say1 应挂起，实得 {first!r}"

    with pytest.raises(RuntimeError, match="挂起"):
        agent.say("second-input")

    answer = agent.resume("yes")
    assert answer == "inp='first-input'", (
        f"resume 必须续跑首个挂起会话，实得 {answer!r}")
    assert arch.compile_inputs == ["first-input", "first-input"], (
        f"say2 不应重建/覆盖首个会话，实得 {arch.compile_inputs}")
