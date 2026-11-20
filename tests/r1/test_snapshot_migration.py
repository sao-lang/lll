"""P5 离线快照迁移工具：旧 kind 快照 → tagged union 形态（一次性、幂等、fail-closed）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from migrate_snapshot import migrate_effect, migrate_snapshot  # noqa: E402

from runtime import Invoke, Suspend, Terminate, restore_checkpoint  # noqa: E402


def _state_snapshot() -> dict:
    """最小 state 快照（满足 restore_state 字段要求）。"""
    return {
        "messages": [], "scratch": {}, "budgets": {}, "version": "0.0.0",
        "output": None, "last_skill": None, "last_child": None,
        "last_wait_human": None, "last_step_kinds": [], "last_tokens": 0,
        "last_reason": "",
    }


class TestSnapshotMigration:
    def test_migrate_invoke_kinds(self) -> None:
        """llm/tool/skill → invoke + port（保留 payload/batch/幂等键）。"""
        assert migrate_effect({
            "kind": "llm", "payload": {"messages": []}, "batch": False,
            "trace_id": "s:0", "idempotency_key": "K",
        }) == {
            "disposition": "invoke", "port": "llm", "payload": {"messages": []},
            "batch": False, "trace_id": "s:0", "idempotency_key": "K",
        }
        assert migrate_effect({"kind": "tool", "payload": {"call": {}}})["port"] == "tool"
        assert migrate_effect({"kind": "skill", "payload": {"name": "x"}})["port"] == "skill"

    def test_migrate_control_effects(self) -> None:
        """child/wait_human/end → spawn/suspend/terminate。"""
        assert migrate_effect({"kind": "child", "payload": {"state": {}}})["disposition"] == "spawn"
        s = migrate_effect({"kind": "wait_human", "payload": {"question": "q", "options": ["a"]}})
        assert s["disposition"] == "suspend" and s["question"] == "q"
        t = migrate_effect({"kind": "end", "payload": {"output": "done", "reason": "ok"}})
        assert t["disposition"] == "terminate" and t["output"] == "done"

    def test_migrate_is_idempotent(self) -> None:
        """已迁移的 Effect 再迁移不变（幂等）。"""
        once = migrate_effect({"kind": "llm", "payload": {}})
        assert migrate_effect(once) == once

    def test_unknown_kind_fails_closed(self) -> None:
        """未知 kind 抛 ValueError（fail-closed，不静默猜测）。"""
        with pytest.raises(ValueError):
            migrate_effect({"kind": "mystery"})

    def test_migrated_snapshot_restores_to_new_types(self) -> None:
        """端到端：旧快照迁移后能被 restore_checkpoint 重建为新 Effect 类型。"""
        st = _state_snapshot()
        old = {
            "session_id": "s", "version": "0.0.0", "fingerprint": "x",
            "state": st,
            "events": [{
                "seq": 0, "session_id": "s", "step": 0, "state_snapshot": st,
                "effects": [
                    {"kind": "llm", "payload": {}, "batch": False, "trace_id": "s:0"},
                    {"kind": "wait_human", "payload": {"question": "q"}},
                    {"kind": "end", "payload": {"output": "done"}},
                ],
                "results": [], "batched": False, "ts": 0, "cost_usd": 0,
                "tokens": 0, "status": "running", "reason": "",
            }],
        }
        new = migrate_snapshot(old)
        session = restore_checkpoint(new, lambda: (yield))
        effects = session.events[0].effects
        assert isinstance(effects[0], Invoke)
        assert isinstance(effects[1], Suspend) and effects[1].question == "q"
        assert isinstance(effects[2], Terminate) and effects[2].output == "done"
