"""examples 层持久化适配器（demo 级，设计文档契约 2026-11-24/react-chat-web）。

框架内核 MemoryStore 是内存 dict（R7 设计：memory 长期知识由注入后端提供）。
本模块在 **examples 层**提供 JSON 文件实现，不改 src 内核：
- FileMemoryStore：继承 MemoryStore，覆写 put_item 落盘 + 构造时 load 恢复；
- FileEventStore：履约 R1 EventStore，把 checkpoint JsonSnapshot 落 JSON，
  让「多轮对话」跨进程重启恢复（checkpoint 与 memory 是两套，互不混淆）。

诚实边界：demo 级本地 JSON，非加密、非并发安全、非生产存储。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from compose import PortCtx
from harness.memory import MemoryItem, MemoryScope, MemoryStore, MemoryTier
from runtime import JsonSnapshot


class FileMemoryStore(MemoryStore):
    """JSON 文件记忆后端：每次写入落盘，构造时从文件恢复。"""

    def __init__(self, path: Path, **kwargs: Any) -> None:
        """构造：先建内存 store，再从 path 恢复（文件不存在则从空开始）。"""
        super().__init__(**kwargs)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def put_item(self, item: MemoryItem, tier: MemoryTier,
                 *, now: int | None = None) -> None:
        """登记并落盘（同 id 覆盖不追加）。"""
        super().put_item(item, tier, now=now)
        self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # 损坏文件不崩溃：从空记忆开始（诚实：demo 级容错）
        for rec in data.get("items", []):
            try:
                scope_raw = rec.get("scope") or {"tenant": "default"}
                scope = MemoryScope(
                    tenant=scope_raw.get("tenant", "default"),
                    user=scope_raw.get("user"), session=scope_raw.get("session"))
                item = MemoryItem(
                    kind=rec["kind"], content=rec["content"],
                    importance=float(rec["importance"]), ts=int(rec["ts"]),
                    source=rec["source"], item_id=rec["item_id"],
                    confidence=float(rec.get("confidence", 1.0)),
                    tier=rec.get("tier", "WARM"),
                    scope=scope,
                    access_count=int(rec.get("access_count", 0)),
                )
                self._items[item.item_id] = item
                self._tiers[item.item_id] = MemoryTier(rec.get("tier_level", "WARM"))
                self._item_times[item.item_id] = item.ts
            except (KeyError, ValueError, TypeError):
                continue  # 跳过坏记录，不污染其他记忆

    def _save(self) -> None:
        items = []
        for item_id, item in self._items.items():
            rec = {
                "kind": str(item.kind), "content": item.content,
                "importance": item.importance, "ts": item.ts,
                "source": item.source, "item_id": item_id,
                "confidence": item.confidence, "tier": str(item.tier),
                "scope": {"tenant": item.scope.tenant, "user": item.scope.user,
                          "session": item.scope.session},
                "access_count": item.access_count,
                "tier_level": str(self._tiers.get(item_id, MemoryTier.WARM)),
            }
            items.append(rec)
        self.path.write_text(
            json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8")


class FileEventStore:
    """R1 EventStore 文件实现：checkpoint JsonSnapshot 落 JSON（多轮对话跨重启）。"""

    name = "file_event_store"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def save(self, session_id: str, snapshot: JsonSnapshot) -> None:
        """落盘（原子替换，避免半写文件）。"""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False, default=str),
            encoding="utf-8")
        tmp.replace(self.path)

    async def load(self, session_id: str) -> JsonSnapshot | None:
        """读回；文件不存在或损坏返回 None（诚实降级，不伪造快照）。"""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data  # type: ignore[return-value]
