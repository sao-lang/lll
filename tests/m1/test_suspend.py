"""M1-T06 挂起 / 断点恢复：wait_human → checkpoint → 续跑（含跨周期重建）。"""
from __future__ import annotations

from runtime import (
    AgentState, Effect, EffectKind, PortMissing, Runner, Session, checkpoint,
    restore_checkpoint,
)
from tests.fakes import FakeCall, FakeDispatch, llm_response, run


def approval_program(state):
    yield Effect(EffectKind.LLM, {"messages": []})
    answer = yield Effect(EffectKind.WAIT_HUMAN, {"question": "允许吗?"})
    yield Effect(EffectKind.END, {"output": answer})


def factory():
    return approval_program(AgentState())


def test_t06_suspend_then_resume():
    session = Session("suspend", factory)
    runner = Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("thinking")]))
    first = run(runner.run(session))
    assert first.status == "suspended" and first.reason == "wait_human"
    assert runner._store.load("suspend") is not None  # 挂起时必存 checkpoint

    resumed = run(runner.resume(session, "approved"))
    assert resumed.status == "done" and resumed.output == "approved"
    assert len(session.events) == 3


def test_t06_cross_cycle_restore_from_snapshot():
    session = Session("cross", factory)
    runner = Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("thinking")]))
    run(runner.run(session))
    snap = checkpoint(session)
    restored = restore_checkpoint(snap, factory)  # 新"进程"：只有快照 + program 工厂

    fresh = Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("thinking")]))
    final = run(fresh.resume(restored, "yes-from-new-process"))
    assert final.status == "done" and final.output == "yes-from-new-process"


def test_t06_run_after_suspend_stays_suspended():
    session = Session("idle", factory)
    runner = Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("x")]))
    run(runner.run(session))
    again = run(runner.run(session))
    assert again.status == "suspended" and len(session.events) == 2


def test_t06_load_without_checkpoint_raises():
    runner = Runner(dispatch=FakeDispatch(), call=FakeCall())
    try:
        runner.load("missing", factory)
        raise AssertionError("应抛 PortMissing")
    except PortMissing:
        pass
