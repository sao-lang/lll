"""R10-T03/T04 回放与 checkpoint：复用 R1 replay_events + EventStore（TDD RED → GREEN）。"""
from __future__ import annotations

import asyncio

from runtime import AgentState, InMemoryEventStore, Result, Terminate, replay_events
from harness.durability import CheckpointManager, CheckpointSnapshot, replay_snapshot


def _state(mark: str) -> AgentState:
    return AgentState().set("scratch", {"mark": mark})


def _event(output: str):
    """构造可 fold 的事件（effects/results/reason），results 用真实 Result。"""
    class Ev:
        effects = (Terminate(output=output),)
        results = (Result.success(None),)
        reason = ""

    return Ev()


def test_t03_replay_reaches_terminal_state() -> None:
    """回放：录制事件重放后 State 与终态全等（复用 R1 replay_events）。"""
    initial = _state("init")
    events = (_event("done"),)
    final = replay_events(events, initial)
    assert final.output == "done"


def test_t04_checkpoint_save_load_roundtrip() -> None:
    """checkpoint：保存快照到 R1 EventStore，load 后结构一致（跨进程语义）。"""
    store = InMemoryEventStore()
    mgr = CheckpointManager(store=store)
    snap = mgr.snapshot(session_id="s10", state=_state("x"), events=(_event("done"),))
    asyncio.run(mgr.save(snap))
    loaded = asyncio.run(mgr.load("s10"))
    assert loaded is not None
    assert loaded.session_id == "s10"
    assert loaded.fingerprint == snap.fingerprint
    assert len(loaded.events) == 1


def test_t04_fingerprint_detects_drift() -> None:
    """fingerprint 随事件变化，防篡改/漂移。"""
    mgr = CheckpointManager()
    s1 = mgr.snapshot("s10", _state("x"), (_event("done"),))
    s2 = mgr.snapshot("s10", _state("x"), (_event("other"),))
    assert s1.fingerprint != s2.fingerprint


def test_t04_replay_snapshot() -> None:
    """replay_snapshot：从快照的 events 重建 State。"""
    mgr = CheckpointManager()
    snap = mgr.snapshot("s10", _state("init"), (_event("final"),))
    final = replay_snapshot(snap)
    assert final.output == "final"
