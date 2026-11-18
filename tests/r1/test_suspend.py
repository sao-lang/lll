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


def test_replay_backfill_branches():
    """resume 回填的核心纯函数：批量取 list、单值取 data、失败取 Result 信封。"""
    from runtime.runner import _send_value

    ok = Result.success("data-val")
    bad = Result.failure("E", "boom")
    base = dict(seq=1, session_id="s", step=0, state_snapshot=AgentState(),
                cost_usd=0.0, tokens=0, ts=0, status="running", reason="")

    from runtime import Event
    # 单值成功 → data
    assert _send_value(Event(**base, effects=(), results=(ok,), batched=False)) == "data-val"
    # 单值失败 → Result 信封本身（Program 能读到 error）
    assert _send_value(Event(**base, effects=(), results=(bad,), batched=False)) is bad
    # 批量 → list，失败项保留信封
    batch_val = _send_value(Event(**base, effects=(), results=(ok, bad), batched=True))
    assert isinstance(batch_val, list) and batch_val[0] == "data-val" and batch_val[1] is bad


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
