"""R10-T03/T04 回放与 checkpoint：复用 R1 replay_events + EventStore（TDD RED → GREEN）。"""
from __future__ import annotations

import asyncio

from runtime import AgentState, Budgets, InMemoryEventStore, Result, Terminate, replay_events
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
    """replay_snapshot：从真实落盘快照（plain dict events）重建终态。

    旧版用内存 Ev 对象（带真属性）自证，掩盖了 load() 返回 plain dict 的现实；
    R1 restore_checkpoint 按 disposition 重建真实 Effect，故这里必须用同构 plain dict。
    """
    store = InMemoryEventStore()
    mgr = CheckpointManager(store=store)
    real_event = {
        "seq": 0, "session_id": "s10", "step": 0,
        "state_snapshot": {
            "messages": [], "scratch": {}, "version": "0.0.0", "output": None,
            "budgets": Budgets().__dict__,
            "last_skill": None, "last_child": None, "last_wait_human": None,
            "last_step_kinds": [], "last_tokens": 0, "last_reason": "",
        },
        "effects": [{"output": "final", "reason": "", "disposition": "terminate"}],
        "results": [{"ok": True, "data": None, "usage": None, "error": None}],
        "batched": False, "ts": 0, "cost_usd": 0, "tokens": 0,
        "status": "done", "reason": "terminated",
    }
    snap = mgr.snapshot("s10", _state("init"), (real_event,))  # type: ignore[arg-type]
    final = replay_snapshot(snap)
    assert final.output == "final"
