"""FR-2/3 RED：跨会话持久化——重启后记忆与多轮历史恢复。

FileMemoryStore：继承 MemoryStore，put_item 时落 JSON，构造时 load 恢复。
FileEventStore：履约 R1 EventStore，save/load JsonSnapshot 落 JSON。
两测试均先 RED（类不存在），实现后转 GREEN。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_red_file_memory_store_survives_restart(tmp_path: Path) -> None:
    """记忆落盘：一个实例写入，另一实例读同一文件能召回（模拟重启）。"""
    from examples.react_chat.persistence import FileMemoryStore
    from harness.memory import MemoryItem, MemoryKind, MemoryScope
    from compose import PortCtx
    from harness.memory import MemoryQuery

    path = tmp_path / "memory.json"
    ctx = PortCtx(session_id="s1")
    store1 = FileMemoryStore(path)
    store1.ingest("用户喜欢喝咖啡不加糖", ctx, confidence=0.9)
    assert store1.tier_count() >= 1

    # 模拟进程重启：全新实例读同一文件
    store2 = FileMemoryStore(path)
    assert store2.tier_count() >= 1, "重启后记忆丢失"
    import asyncio
    hits = asyncio.run(store2.recall(MemoryQuery(text="咖啡", top_k=3), ctx))
    assert any("咖啡" in str(getattr(h, "content", "")) for h in hits), (
        "重启后无法召回已落盘记忆")


def test_red_file_event_store_saves_json_snapshot(tmp_path: Path) -> None:
    """多轮历史：R1 EventStore save/load 往返一致（JSON 落地）。"""
    import asyncio
    from examples.react_chat.persistence import FileEventStore

    path = tmp_path / "events.json"
    store = FileEventStore(path)
    snapshot = {"events": [{"seq": 1, "kind": "user", "payload": {"text": "你好"}}],
                "state": {"session_id": "s1"}}
    asyncio.run(store.save("s1", snapshot))  # type: ignore[arg-type]
    loaded = asyncio.run(store.load("s1"))
    assert loaded is not None and loaded["events"][0]["payload"]["text"] == "你好"
    assert path.exists() and json.loads(path.read_text(encoding="utf-8"))


def test_red_file_memory_store_session_isolated(tmp_path: Path) -> None:
    """会话隔离：不同 session 的记忆文件互不污染。"""
    from examples.react_chat.persistence import FileMemoryStore
    from compose import PortCtx

    a = FileMemoryStore(tmp_path / "alice.json")
    b = FileMemoryStore(tmp_path / "bob.json")
    a.ingest("alice 住在北京", PortCtx(session_id="alice"))
    assert b.tier_count() == 0, "不同会话的记忆串了"
