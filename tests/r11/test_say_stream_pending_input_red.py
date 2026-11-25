"""RED-Bug1：say_stream 挂起必须保存 _pending_input 并下发 [wait_human]。

复审复现 tmp/review_pending2.py：say_stream.drive() 只 await _arun()，从不写
_pending_input（asay 写了），aresume 用空串重建 ContextVar → 续跑 compile 读到
空输入；且挂起时流静默结束，调用方无从得知需先 resume（fail-closed 守卫随后
静默拒绝第二次 say_stream）。修后必须：①流里有 [wait_human] 提示；②原输入被
保存；③resume 读到原输入；④守卫拒绝不覆盖已保存输入。
"""
from __future__ import annotations

import asyncio

from api import Agent
from compose import LLMResponse, Usage, current_input
from harness.model import ModelCapability
from runtime import Invoke, Program, Suspend, Terminate


class _SuspendArch:
    """compile 期读 current_input()，Suspend 后用其产出 Terminate。"""

    name = "susp"

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
                                       cost_usd=0.001), model=self.name)

    async def stream(self, req: object, ctx: object) -> object:
        raise NotImplementedError


def test_red_say_stream_suspension_keeps_input_and_signals() -> None:
    arch = _SuspendArch()
    agent = Agent(arch, session_id="say-stream-pending-red")  # type: ignore[arg-type]
    agent.use(ModelCapability(llm=_EchoLLM()))

    async def scenario() -> None:
        chunks = [c async for c in agent.say_stream("first")]
        assert any("[wait_human]" in c for c in chunks), (
            f"挂起必须向流下发 [wait_human] 提示，实际 {chunks!r}")
        assert agent._pending_input == "first", (
            f"挂起原输入必须保存供 resume 重建 ContextVar，实际 {agent._pending_input!r}")
        assert agent.last_run is not None and agent.last_run.status == "suspended"

        # fail-closed：挂起后第二次 say_stream 必须被拒，且不覆盖已保存输入
        refusal = ""
        try:
            async for _ in agent.say_stream("second"):
                pass
        except RuntimeError as exc:
            refusal = str(exc)
        assert "未恢复的挂起会话" in refusal, (
            f"第二次 say_stream 未被守卫拒绝：{refusal!r}")
        assert agent._pending_input == "first", "守卫拒绝不得覆盖挂起输入"

        answer = await agent.aresume("yes")
        assert answer == "inp='first'", f"续跑必须读到原输入 first，实际 {answer!r}"
        assert agent._pending_input == ""
        assert arch.compile_inputs == ["first", "first"], arch.compile_inputs

    asyncio.run(asyncio.wait_for(scenario(), timeout=6))
