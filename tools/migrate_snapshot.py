"""离线快照迁移工具（P5）：把旧 checkpoint 快照的 `kind` 字段一次性转为 tagged union 形态。

不做运行时兼容层——本工具是**离线一次性迁移**，输入旧快照 JSON，输出新快照 JSON。
旧 Effect 形态：{"kind": "llm"|"tool"|"skill"|"child"|"wait_human"|"end", "payload": {...}, ...}
新 Effect 形态：
- llm/tool/skill → {"disposition": "invoke", "port": "...", "payload": {...}, ...}
- child         → {"disposition": "spawn", "state": {...}}（program_factory 不可序列化，留空由恢复重注入）
- wait_human    → {"disposition": "suspend", "question": "...", "options": [...]}
- end           → {"disposition": "terminate", "output": ..., "reason": "..."}

用法：
    python tools/migrate_snapshot.py <旧快照.json> <新快照.json>
    python tools/migrate_snapshot.py --self-test
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["migrate_snapshot", "migrate_effect"]

_KIND_TO_PORT = {"llm": "llm", "tool": "tool", "skill": "skill"}


def migrate_effect(old: dict[str, Any]) -> dict[str, Any]:
    """把一个旧 Effect 字典迁移为新 tagged union 字典。

    参数：
        old: 旧快照中的 Effect（含 kind 判别）。
    返回：
        新形态 Effect（含 disposition 判别）。
    说明：
        已是新形态（含 disposition）时原样返回（幂等）；未知 kind 抛 ValueError（fail-closed）。
    """
    if "disposition" in old:
        return dict(old)  # 幂等：已迁移
    kind = str(old.get("kind", ""))
    payload = old.get("payload") or {}
    common = {k: old[k] for k in ("batch", "trace_id", "idempotency_key") if k in old}

    if kind in _KIND_TO_PORT:
        return {
            "disposition": "invoke", "port": _KIND_TO_PORT[kind],
            "payload": dict(payload), **common,
        }
    if kind == "child":
        # program_factory 不可序列化：丢弃，state 保留（恢复时由 program_factory 重注入）。
        migrated: dict[str, Any] = {"disposition": "spawn"}
        if "state" in payload:
            migrated["state"] = payload["state"]
        if "trace_id" in old:
            migrated["trace_id"] = old["trace_id"]
        return migrated
    if kind == "wait_human":
        migrated = {"disposition": "suspend"}
        for key in ("question", "options"):
            if key in payload:
                migrated[key] = payload[key]
        if "trace_id" in old:
            migrated["trace_id"] = old["trace_id"]
        return migrated
    if kind == "end":
        migrated = {"disposition": "terminate"}
        for key in ("output", "reason"):
            if key in payload:
                migrated[key] = payload[key]
        if "trace_id" in old:
            migrated["trace_id"] = old["trace_id"]
        return migrated
    raise ValueError(f"unknown legacy effect kind: {kind!r}")


def migrate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """迁移整份 checkpoint 快照（events[].effects 中的旧 Effect）。

    参数：
        snapshot: 旧快照 JSON（dict）。
    返回：
        新快照 JSON（dict）；顶层其他字段（state/version/fingerprint）原样保留。
    """
    result = dict(snapshot)
    events = result.get("events")
    if not isinstance(events, list):
        return result
    new_events = []
    for event in events:
        ev = dict(event)
        effects = ev.get("effects")
        if isinstance(effects, list):
            ev["effects"] = [migrate_effect(e) if isinstance(e, dict) else e for e in effects]
        new_events.append(ev)
    result["events"] = new_events
    return result


def _self_test() -> None:
    """内置自检（无需 pytest）：覆盖四类迁移与幂等。"""
    old_snapshot = {
        "session_id": "s", "version": "0.0.0", "fingerprint": "abc",
        "state": {"messages": []},
        "events": [{
            "effects": [
                {"kind": "llm", "payload": {"messages": []}, "batch": False},
                {"kind": "tool", "payload": {"call": {}}, "idempotency_key": "K"},
                {"kind": "wait_human", "payload": {"question": "q?", "options": ["a"]}},
                {"kind": "end", "payload": {"output": "done"}},
            ]
        }],
    }
    new = migrate_snapshot(old_snapshot)
    effects = new["events"][0]["effects"]
    assert [e["disposition"] for e in effects] == ["invoke", "invoke", "suspend", "terminate"]
    assert effects[0]["port"] == "llm" and effects[1]["idempotency_key"] == "K"
    assert effects[2]["question"] == "q?"
    assert effects[3]["output"] == "done"
    # 幂等：再迁移一次不变
    assert migrate_snapshot(new) == new
    # fail-closed：未知 kind 抛错
    try:
        migrate_effect({"kind": "mystery"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown kind must raise")
    print("PASS migrate_snapshot: 四类迁移 + 幂等 + fail-closed")


def main() -> int:
    """命令行入口：迁移快照文件或自检。"""
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        _self_test()
        return 0
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    old = json.loads(src.read_text(encoding="utf-8"))
    new = migrate_snapshot(old)
    dst.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"migrated {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
