"""R7 MemoryPort 默认实现：三层三类记忆、注入/secret 防线与语义召回。

- read/write/forget 履约 R2 MemoryPort（namespace/key 存取）。
- put_item/tier_of/promote/demote/forget_expired 管理 hot/warm/cold 分层。
- recall 经注入的 Embedder + VectorStore 做语义召回，返回 R7 自有 MemoryItem。
- 写入前复用 R5 注入检测 + secret 键名判定，命中即拒绝（fail-closed）。
"""
from __future__ import annotations

from hooks import PortCtx
from harness.rag.embedder import Embedder
from harness.rag.store import VectorStore
from harness.rag.types import Chunk
from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector
from runtime import DynamicValue

from .types import MemoryItem, MemoryKind, MemoryQuery, MemoryTier, is_secret_key

__all__ = ["MemoryStore"]

# 分层顺序：索引越小越"热"（会话内），越大越"冷"（归档）。
_TIER_ORDER: tuple[MemoryTier, ...] = (MemoryTier.HOT, MemoryTier.WARM, MemoryTier.COLD)
_RECALL_CHUNK_PREFIX = "mem:"


class MemoryStore:
    """R7 记忆默认实现：namespace 键值 + 三层分层 + 语义召回。"""

    name: str = "memory_store"

    def __init__(
        self,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        detector: InjectionDetector | None = None,
    ) -> None:
        """构造记忆存储。

        Args:
            embedder: 向量化后端；None 时用 R6 HashEmbedder。
            vector_store: 向量库；None 时用 R6 InMemoryVectorStore。
            detector: 注入检测器；None 时用 R5 KeywordInjectionDetector。
        """
        from harness.rag.embedder import HashEmbedder
        from harness.rag.store import InMemoryVectorStore

        self._embedder = embedder if embedder is not None else HashEmbedder()
        self._vstore = vector_store if vector_store is not None else InMemoryVectorStore()
        self._detector = detector if detector is not None else KeywordInjectionDetector()
        self._kv: dict[tuple[str, str], DynamicValue] = {}
        self._tiers: dict[int, MemoryTier] = {}
        self._items: dict[int, MemoryItem] = {}
        self._item_times: dict[int, int] = {}
        self._next_item_id: int = 0

    async def read(self, namespace: str, key: str, ctx: PortCtx) -> DynamicValue:
        """按 namespace/key 读取；不存在返回 None。"""
        return self._kv.get((namespace, key))

    async def write(self, namespace: str, key: str, value: DynamicValue, ctx: PortCtx) -> None:
        """写入记忆；secret 键名或注入串内容拒绝（fail-closed）。"""
        if is_secret_key(key):
            raise ValueError(f"secret key rejected: {key}")
        if isinstance(value, str) and self._is_injected(value):
            raise ValueError("injected content rejected")
        self._kv[(namespace, key)] = value

    async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:
        """遗忘指定键值；不存在幂等。"""
        self._kv.pop((namespace, key), None)

    async def recall(self, query: MemoryQuery, ctx: PortCtx) -> list[MemoryItem]:
        """语义召回：嵌入全部记忆项→向量检索→映射回 MemoryItem。

        无 embedder/vstore 时退化为按 importance 降序（显式降级，不静默）。
        """
        items = list(self._items.values())
        if query.tier is not None:
            items = [it for it in items if self.tier_of(it) is query.tier]
        if query.kind is not None:
            items = [it for it in items if it.kind is query.kind]
        if not items:
            return []
        vectors = await self._embedder.embed([it.content for it in items])
        chunks = [
            Chunk(
                chunk_id=f"{_RECALL_CHUNK_PREFIX}{i}",
                doc_id="memory",
                text=it.content,
                vector=vec,
            )
            for i, (it, vec) in enumerate(zip(items, vectors))
        ]
        await self._vstore.upsert(chunks)
        qvec = (await self._embedder.embed([query.text]))[0]
        hits = await self._vstore.search(qvec, query.top_k)
        by_id = {f"{_RECALL_CHUNK_PREFIX}{i}": it for i, it in enumerate(items)}
        out: list[MemoryItem] = []
        for hit in hits:
            item = self._item_from_hit(hit, by_id)
            if item is not None:
                out.append(item)
        return out

    def write_cold(self, transcript: str, ctx: PortCtx | None = None) -> None:
        """写 cold 全文归档（编排器 commit_and_compact 第一步调用）。"""
        item = MemoryItem(
            kind=MemoryKind.EPISODIC,
            content=transcript,
            importance=1.0,
            ts=0,
            source="cold",
        )
        self.put_item(item, MemoryTier.COLD)

    def put_item(
        self, item: MemoryItem, tier: MemoryTier, *, now: int | None = None
    ) -> None:
        """登记一条记忆项到指定层。"""
        item_id = self._next_item_id
        self._next_item_id += 1
        self._items[item_id] = item
        self._tiers[item_id] = tier
        self._item_times[item_id] = now if now is not None else item.ts

    def tier_of(self, item: MemoryItem) -> MemoryTier | None:
        """查记忆项当前所在层；未登记返回 None。"""
        for item_id, stored in self._items.items():
            if stored is item:
                return self._tiers.get(item_id)
        return None

    def tier_count(self) -> int:
        """返回已登记记忆项总数（诊断/测试用）。"""
        return len(self._items)

    def promote(self, item: MemoryItem) -> None:
        """加深归档一级：hot→warm→cold；已在 cold 则不动。"""
        self._shift(item, +1)

    def demote(self, item: MemoryItem) -> None:
        """回温一级：cold→warm→hot；已在 hot 则不动。"""
        self._shift(item, -1)

    def forget_expired(self, ttl: int, *, now: int) -> list[MemoryItem]:
        """遗忘过期项（now - 写入时刻 > ttl），返回被遗忘项列表。"""
        expired: list[MemoryItem] = []
        for item_id, ts in list(self._item_times.items()):
            if now - ts > ttl:
                item = self._items.pop(item_id, None)
                self._tiers.pop(item_id, None)
                self._item_times.pop(item_id, None)
                if item is not None:
                    expired.append(item)
        return expired

    def _shift(self, item: MemoryItem, delta: int) -> None:
        """按 delta 在当前层序上移动一级（+ 加深 / - 回温）。"""
        for item_id, stored in self._items.items():
            if stored is not item:
                continue
            current = self._tiers.get(item_id)
            if current is None:
                return
            idx = _TIER_ORDER.index(current)
            new_idx = min(max(idx + delta, 0), len(_TIER_ORDER) - 1)
            self._tiers[item_id] = _TIER_ORDER[new_idx]
            return

    def _is_injected(self, text: str) -> bool:
        """复用 R5 检测器判定注入串。"""
        return self._detector.detect(text) is InjectionVerdict.INJECTED

    @staticmethod
    def _item_from_hit(hit: object, by_id: dict[str, MemoryItem]) -> MemoryItem | None:
        """从向量命中提取 chunk_id 映射回 MemoryItem。"""
        chunk = getattr(hit, "chunk", None)
        chunk_id = getattr(chunk, "chunk_id", None)
        if not isinstance(chunk_id, str):
            return None
        return by_id.get(chunk_id)
