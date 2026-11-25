"""用户侧自定义记忆后端：真 KV + 真词面召回 + 调用计数。

履约 MemoryPort(read/write/forget) + MemoryPortWithRecall(recall)。
非测试桩：write 真落词面索引，recall 真按词面重叠打分返回，返回值可被门面注入 LLM。
副作用：write_calls / recall_calls / ingest_calls，供双实现差分断言。
"""
from __future__ import annotations

from compose import PortCtx
from harness.memory.types import MemoryItem, MemoryQuery, MemoryKind, MemoryTier, make_item_id


class UserMemory:
    """用户自定义记忆实现（非框架类），真词面索引。"""

    name = "user_memory"

    def __init__(self) -> None:
        self._kv: dict[tuple[str, str], object] = {}
        self._docs: list[tuple[str, MemoryItem]] = []
        self.write_calls = 0
        self.read_calls = 0
        self.recall_calls = 0
        self.forget_calls = 0
        self.ingest_calls = 0

    async def read(self, namespace: str, key: str, ctx: PortCtx) -> object:
        self.read_calls += 1
        return self._kv.get((namespace, key))

    async def write(self, namespace: str, key: str, value: object, ctx: PortCtx) -> None:
        self.write_calls += 1
        self._kv[(namespace, key)] = value
        text = value if isinstance(value, str) else str(value)
        if text.strip():
            self.ingest_calls += 1
            tenant = str((ctx.meta or {}).get("scope").tenant) if hasattr((ctx.meta or {}).get("scope"), "tenant") else "default"
            import time
            item = MemoryItem(
                kind=MemoryKind.SEMANTIC, content=text, importance=1.0,
                ts=time.time_ns(), source=f"write:{namespace}/{key}",
                item_id=make_item_id(tenant, MemoryKind.SEMANTIC, text), confidence=1.0,
                tier=MemoryTier.WARM, scope=_scope(tenant),
            )
            self._docs.append((text, item))

    def ingest(self, text: str, ctx: PortCtx, *, confidence: float = 1.0,
               source: str = "user-ingest") -> object:
        """门面 write_after 契约：真建召回条目（非桩）。"""
        import time
        self.ingest_calls += 1
        tenant = str((ctx.meta or {}).get("scope").tenant) if hasattr((ctx.meta or {}).get("scope"), "tenant") else "default"
        item = MemoryItem(
            kind=MemoryKind.SEMANTIC, content=text, importance=1.0,
            ts=time.time_ns(), source=source,
            item_id=make_item_id(tenant, MemoryKind.SEMANTIC, text), confidence=confidence,
            tier=MemoryTier.WARM, scope=_scope(tenant),
        )
        self._docs.append((text, item))
        return item

    async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:
        self.forget_calls += 1
        self._kv.pop((namespace, key), None)

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """中文按字符 bigram 切词（真实词面索引口径，无空格也可召回）。"""
        chars = [c for c in text if not c.isspace()]
        return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)} if len(chars) >= 2 else set(chars)

    async def recall(self, query: MemoryQuery, ctx: PortCtx) -> list[MemoryItem]:
        self.recall_calls += 1
        qgrams = self._bigrams(query.text)
        scored = []
        for text, item in self._docs:
            overlap = len(qgrams & self._bigrams(text))
            if overlap:
                scored.append((overlap, item))
        scored.sort(key=lambda p: p[0], reverse=True)
        return [item for _, item in scored[: query.top_k]]

    def tier_count(self) -> int:
        return len(self._docs)


def _scope(tenant: str) -> object:
    from harness.memory.types import MemoryScope
    return MemoryScope(tenant=tenant)
