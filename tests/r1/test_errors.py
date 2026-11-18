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


def test_invalid_program_run_does_not_raise():
    """B4：run() 构造期 ProgramInvalid 不得抛穿，须折叠为 error RunResult。"""
    async def run():
        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        # 非 generator（list 无 send）：InterpreterImpl 构造即抛 ProgramInvalid
        session = Session(session_id="e-run", program_factory=lambda: [1, 2, 3])
        result = await runner.run(session)
        assert result.status == "error"
        assert result.error == "PROGRAM_INVALID"
    asyncio.run(run())


def test_invalid_program_resume_does_not_raise():
    """B4：resume() 构造期 ProgramInvalid 不得抛穿，须折叠为 error RunResult。"""
    async def run():
        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        session = Session(session_id="e-resume", program_factory=lambda: [1, 2, 3])
        result = await runner.resume(session, answer="ok")
        assert result.status == "error"
        assert result.error == "PROGRAM_INVALID"
    asyncio.run(run())


def test_mixed_batch_executes_all_effects():
    """B8：混合批次 [END, LLM] 不得只看 effects[0] 吞掉后续 Effect。

    要么整批都是 END/WAIT_HUMAN 才走快路径；否则每个 Effect 都必须执行，
    Event 的 effects 与 results 必须等长配对。
    """
    async def run():
        calls = []

        async def counting_call(effect, ctx):
            calls.append(effect.kind)
            return Result.success("ok")

        def program(state):
            yield [Effect(EffectKind.END, {"output": "x"}), Effect(EffectKind.LLM, {"messages": []})]

        runner = Runner(dispatch=fake_dispatch, call=counting_call)
        session = Session(session_id="mixed", program_factory=lambda: program(AgentState()))
        await runner.step(session)
        # END 快路径不应吞掉同批的 LLM；effects 与 results 等长。
        event = session.events[0] if session.events else None
        assert event is not None
        assert len(event.effects) == len(event.results), \
            f"effects/results 不配对：{len(event.effects)} vs {len(event.results)}"
    asyncio.run(run())
