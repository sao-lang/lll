"""确认 bug #3：混合批中 Suspend/Terminate 被误送 Port（audit_mixed_batch.py）。"""
from __future__ import annotations

import asyncio

from runtime import AgentState, Invoke, Result, Runner, Session, Suspend, Terminate


def _recording_call(calls: list[str]):
    async def call(effect, ctx) -> Result:
        calls.append(type(effect).__name__)
        return Result.success("ok")
    return call


def test_mixed_invoke_suspend_suspends_without_port_calling_suspend() -> None:
    """[Invoke, Suspend] 必须挂起，且 Suspend 绝不进入 Port 调用。"""
    async def run() -> None:
        calls: list[str] = []

        def program(state):
            yield [Invoke(port="llm", payload={"x": 1}), Suspend(question="approve?")]
            yield Terminate(output="done")

        session = Session(session_id="mix-suspend", program_factory=lambda: program(AgentState()))
        runner = Runner(call=_recording_call(calls))

        result = await runner.run(session)

        assert result.status == "suspended", f"status={result.status}, 期望 suspended"
        assert calls == ["Invoke"], \
            f"Port 只应收到 Invoke，实际收到 {calls}（Suspend 被当 Port 调用）"
        assert len(session.events[0].effects) == 2
        assert len(session.events[0].results) == 2
        assert session.events[0].status == "suspended"
        assert session.events[0].results[1].ok is True

        resumed = await runner.resume(session, answer="yes")
        assert resumed.status == "done", f"resume 后 status={resumed.status}"
        assert resumed.output == "done", f"resume 后 output={resumed.output!r}"

    asyncio.run(run())


def test_mixed_invoke_terminate_terminates_without_port_calling_terminate() -> None:
    """[Invoke, Terminate] 必须终止并产出 output，且 Terminate 绝不进入 Port 调用。"""
    async def run() -> None:
        calls: list[str] = []

        def program(state):
            yield [Invoke(port="llm", payload={"x": 1}), Terminate(output="MIX")]

        session = Session(session_id="mix-terminate", program_factory=lambda: program(AgentState()))
        runner = Runner(call=_recording_call(calls))

        result = await runner.run(session)

        assert result.status == "done", f"status={result.status}, 期望 done"
        assert result.output == "MIX", f"output={result.output!r}, 期望 'MIX'"
        assert calls == ["Invoke"], \
            f"Port 只应收到 Invoke，实际收到 {calls}（Terminate 被当 Port 调用）"
        assert len(session.events[0].effects) == 2
        assert len(session.events[0].results) == 2
        assert session.events[0].status == "done"

    asyncio.run(run())
