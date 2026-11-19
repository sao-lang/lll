"""R6 存储段：VectorStore 协议 + 内存默认实现。

向量存放在 Chunk.vector 上（入库前由 Embedder 填充），search 用点积排序，
filters 按 Chunk.meta 的键值对做精确匹配。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.types import Chunk, Hit, Vector

__all__ = ["VectorStore", "InMemoryVectorStore", "matches_filters"]

# 元数据过滤中可直查的块字段（具名常量，避免裸串散落）。
_CHUNK_FIELDS: frozenset[str] = frozenset({"chunk_id", "doc_id"})


def matches_filters(chunk: Chunk, filters: dict[str, object] | None) -> bool:
    """检查块是否满足过滤条件——向量通道与关键词通道共用的唯一判定。

    过滤键若为 ``chunk_id``/``doc_id`` 则直查块字段，否则查 chunk.meta。
    全部门槛都命中才通过（AND 语义）；filters 为 None 表示不过滤。
    """
    if not filters:
        return True
    for key, value in filters.items():
        if key in _CHUNK_FIELDS:
            actual = chunk.chunk_id if key == "chunk_id" else chunk.doc_id
            if actual != value:
                return False
        elif chunk.meta.get(key) != value:
            return False
    return True


@runtime_checkable
class VectorStore(Protocol):
    """向量存储协议（R6 五段之一）。"""

    name: str

    async def upsert(self, chunks: list[Chunk]) -> None:
        """写入块（含其向量）。"""
        ...

    async def search(
        self, vec: Vector, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Hit]:
        """按向量相似度返回 top_k 命中。"""
        ...


class InMemoryVectorStore:
    """内存向量库默认实现：进程内列表存储，点积相似度。"""

    name: str = "in_memory_store"

    def __init__(self) -> None:
        """初始化空库。"""
        self._chunks: list[Chunk] = []

    async def upsert(self, chunks: list[Chunk]) -> None:
        """追加块引用到库中（当前为追加语义，按 chunk_id 覆盖的增量去重未实现）。

        Note:
            存的是对象引用而非深拷贝——调用方入库后不应再修改这些 Chunk；
            需要隔离时可自行传入 model_copy 后的块。
        """
        self._chunks.extend(chunks)

    def all_chunks(self) -> list[Chunk]:
        """返回库内全部块（供混合召回的关键词通道做词面打分）。"""
        return list(self._chunks)

    async def search(
        self, vec: Vector, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Hit]:
        """点积相似度排序，按 meta 过滤后取 top_k。"""
        scored: list[Hit] = []
        for chunk in self._chunks:
            if chunk.vector is None:
                continue
            if not matches_filters(chunk, filters):
                continue
            scored.append(
                Hit(
                    chunk=chunk,
                    score=self._dot(vec, chunk.vector),
                    source=self.name,
                )
            )
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    @staticmethod
    def _dot(a: Vector, b: Vector) -> float:
        """计算两个向量的点积。"""
        return sum(x * y for x, y in zip(a, b))
