"""RED-Bug3: aresume 必须用挂起时保存的 _pending_input 重新 bind_input。

审计 P9a：Runner.resume 会重新 program_factory()，compile 期 current_input()
为空 → ReAct/PlanExecute/Reflection/Router 续跑后提示词变空。
正确修法：aresume 用 bind_input(_pending_input) 包住 rt.resume。
"""
from __future__ import annotations

from api import Agent
from compose import LLMResponse, Usage, current_input
from harness.model import ModelCapability
from runtime import Invoke, Program, Suspend, Terminate


class _InputSensitiveArch:
    """compile 时读 current_input()，Suspend 后再用一次。"""

    name = "input_sensitive"

    def __init__(self) -> None:
        self.compile_inputs: list[str] = []

    def compile(self) -> Program:
        inp = current_input()
        self.compile_inputs.append(inp)

        def program():
            first = yield Invoke(port="llm", payload={
                "messages": [{"role": "user", "content": inp}]})
            yield Suspend(question="approve?", options=["y", "n"])
            yield Terminate(output=f"inp={inp!r};ans={first!r}")
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


def test_red_aresume_rebinds_original_input() -> None:
    """HITL 续跑后 compile 必须读到原 say 输入，不是空串。"""
    arch = _InputSensitiveArch()
    agent = Agent(arch, session_id="bug3-input")  # type: ignore[arg-type]
    agent.use(ModelCapability(llm=_EchoLLM()))

    first = agent.say("original-input")
    assert "wait_human" in first, f"应挂起，实得 {first!r}"
    assert arch.compile_inputs == ["original-input"], arch.compile_inputs

    answer = agent.resume("yes")

    assert answer == "inp='original-input';ans={'content': 'FIRST', 'tool_calls': []}", (
        f"续跑 compile 必须保留原输入，实得 {answer!r}")
    assert arch.compile_inputs == ["original-input", "original-input"], (
        f"compile 输入序列错：{arch.compile_inputs}")
    assert agent.last_run is not None and agent.last_run.status == "done"
