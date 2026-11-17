"""R1-T07：4 类错误进 StepReport 不抛穿。"""
import asyncio

from runtime import AgentState, Effect, EffectKind, ProgramInvalid, Result, Runner, Session


async def fake_call(effect, ctx):
    return Result.failure("BOOM", "crashed", retryable=True)


async def fake_dispatch(effect, ctx):
    return None


def test_port_error_folds_into_result():
    async def run():
        def program(state):
            r = yield Effect(EffectKind.TOOL, {"call": {}})
            yield Effect(EffectKind.END, {"output": r.error.code if hasattr(r, "error") else "ok"})
        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        session = Session(session_id="e1", program_factory=lambda: program(AgentState()))
        result = await runner.run(session)
        assert result.status == "done"
        assert result.output == "BOOM"
    asyncio.run(run())


def test_invalid_program_reports_error():
    async def run():
        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        session = Session(session_id="e2", program_factory=lambda: (i for i in [1, 2, 3]))
        report = await runner.step(session)
        assert report.status == "error"
        assert report.error == "PROGRAM_INVALID"
    asyncio.run(run())
