"""确认 bug #4：Port 返回畸形值异常穿透（audit_cancel_malformed.py）。"""
from __future__ import annotations

import asyncio

from runtime import AgentState, Invoke, Runner, Session, Terminate


def _program(state):
    yield Invoke(port="llm", payload={})
    yield Terminate(output="done")


def test_sync_malformed_port_return_is_folded_not_thrown() -> None:
    """Port 同步返回字符串不得让 run() 抛 AttributeError。"""
    async def run() -> None:
        runner = Runner(call=lambda effect, ctx: "not a Result")
        session = Session(session_id="mal-sync", program_factory=lambda: _program(AgentState()))

        result = await runner.run(session)  # 不得抛异常

        assert result.status == "done", f"status={result.status}"
        assert len(result.events) == 2
        first = result.events[0].results
        assert len(first) == 1
        assert first[0].ok is False
        assert first[0].error is not None
        assert first[0].error.code == "PORT_CRASHED", \
            f"error.code={first[0].error.code!r}, 期望 'PORT_CRASHED'"
        assert first[0].error.message == "port returned str, expected Result", \
            f"error.message={first[0].error.message!r}"

    asyncio.run(run())


def test_async_malformed_port_return_is_folded_not_thrown() -> None:
    """Port 异步返回非 Result 值同样必须在 _execute 边界被折叠。"""
    async def run() -> None:
        async def bad_call(effect, ctx):
            return 42

        runner = Runner(call=bad_call)
        session = Session(session_id="mal-async", program_factory=lambda: _program(AgentState()))

        result = await runner.run(session)  # 不得抛异常

        assert result.status == "done"
        first = result.events[0].results
        assert len(first) == 1
        assert first[0].ok is False
        assert first[0].error is not None
        assert first[0].error.code == "PORT_CRASHED"
        assert first[0].error.message == "port returned int, expected Result"

    asyncio.run(run())
