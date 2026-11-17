"""R1-T06：wait_human 挂起 → checkpoint → resume 续跑。"""
import asyncio

from runtime import AgentState, Effect, EffectKind, Result, Runner, Session


async def fake_call(effect, ctx):
    return Result.success(None)


async def fake_dispatch(effect, ctx):
    return None


def human_program(state):
    answer = yield Effect(EffectKind.WAIT_HUMAN, {"question": "continue?"})
    yield Effect(EffectKind.END, {"output": answer})


def test_suspend_and_resume():
    async def run():
        runner = Runner(dispatch=fake_dispatch, call=fake_call, checkpoint_every=1)
        session = Session(session_id="h1", program_factory=lambda: human_program(AgentState()))
        r1 = await runner.run(session)
        assert r1.status == "suspended"
        assert await runner._store.load("h1") is not None  # checkpoint 已落
        r2 = await runner.resume(session, "yes")
        assert r2.status == "done"
        assert r2.output == "yes"
    asyncio.run(run())
