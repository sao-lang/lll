"""R6 召回段：Retriever 协议 + 向量召回 / 混合召回（RRF 融合）默认实现。

关键设计（B1 修复）：查询向量由 RAGPipeline 用**与入库同一 embedder**算好后
经 ``retrieve(query, vec)`` 注入，召回器绝不自行向量化——避免入库/查询落在不同
语义空间导致检索失效。关键词通道与向量通道共用 ``matches_filters``（B2 修复）。

RRF（Reciprocal Rank Fusion）把不同量纲的通道分数按排名融合：
RRF(d) = Σ weight / (k + rank)，k=60（设计 §5）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.store import VectorStore, matches_filters
from harness.rag.types import Chunk, Hit, Query, RRF_K, Vector

__all__ = ["Retriever", "VectorRetriever", "HybridRetriever"]

# 混合召回的通道权重（具名常量，避免裸数字散落）。
_VECTOR_WEIGHT: float = 1.0
_KEYWORD_WEIGHT: float = 1.0


@runtime_checkable
class Retriever(Protocol):
    """召回协议（R6 五段之一）。

    查询向量由 pipeline 用注入的 embedder 预算好传入，召回器不自行向量化。
    """

    name: str

    async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:
        """用给定查询向量召回命中列表。"""
        ...


class VectorRetriever:
    """纯向量召回：用 pipeline 传入的查询向量走 store.search。"""

    name: str = "vector_retriever"

    def __init__(self, store: VectorStore) -> None:
        """构造召回器，可注入 store。"""
        self._store = store

    async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:
        """用查询向量召回 top_k，filters 在 store 层生效。"""
        return await self._store.search(vec, query.top_k, query.filters)


class HybridRetriever:
    """混合召回：向量通道 + 词面关键词通道，RRF 融合。

    两路都施加同一 ``filters``（B2：关键词通道不再绕过权限/时效过滤）。
    关键词素材优先用构造期显式传入的 all_chunks；未传则从 store 的
    ``all_chunks()`` 鸭子取用；两者皆无时**显式抛错**而非静默退化为纯向量
    （S2：退化必须可见）。
    """

    name: str = "hybrid_retriever"

    def __init__(self, store: VectorStore, *, all_chunks: list[Chunk] | None = None) -> None:
        """构造混合召回器。

        Args:
            store: 向量库（提供向量通道）。
            all_chunks: 关键词通道的倒排素材；None 时从 store.all_chunks() 取。

        Raises:
            ValueError: 无关键词素材来源（既未传 all_chunks，store 也无该能力）。
        """
        self._store = store
        self._all_chunks = all_chunks

    async def retrieve(self, query: Query, vec: Vector) -> list[Hit]:
        """两路召回后 RRF 融合去重，两路都施加 filters。"""
        vector_hits = await self._store.search(vec, query.top_k, query.filters)
        corpus = self._corpus()
        keyword_hits = self._keyword_search(query, corpus)
        fused = self._rrf([vector_hits, keyword_hits])
        return fused[: query.top_k]

    def _corpus(self) -> list[Chunk]:
        """解析关键词通道素材来源；缺失则显式报错（不允许静默退化）。"""
        if self._all_chunks is not None:
            return self._all_chunks
        getter = getattr(self._store, "all_chunks", None)
        if callable(getter):
            return list(getter())
        raise ValueError(
            "混合召回需要关键词素材：请给 HybridRetriever 传 all_chunks，"
            "或让 VectorStore 实现 all_chunks()"
        )

    def _keyword_search(self, query: Query, corpus: list[Chunk]) -> list[Hit]:
        """词面覆盖度打分：先按 filters 过滤，再算 query 词项命中比例。"""
        terms = {w for w in query.text.lower().split() if w}
        if not terms or not corpus:
            return []
        hits: list[Hit] = []
        for chunk in corpus:
            if not matches_filters(chunk, query.filters):
                continue
            body = chunk.text.lower()
            covered = sum(1 for t in terms if t in body)
            score = covered / len(terms)
            if score > 0:
                hits.append(Hit(chunk=chunk, score=score, source=self.name))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: query.top_k]

    @staticmethod
    def _rrf(channels: list[list[Hit]]) -> list[Hit]:
        """按 RRF 融合多通道，按 chunk_id 去重，分数写回 score。"""
        weights = (_VECTOR_WEIGHT, _KEYWORD_WEIGHT)
        acc: dict[str, tuple[Hit, float]] = {}
        for idx, hits in enumerate(channels):
            weight = weights[idx] if idx < len(weights) else _VECTOR_WEIGHT
            for rank, hit in enumerate(hits, start=1):
                cid = hit.chunk.chunk_id
                contribution = weight / (RRF_K + rank)
                if cid in acc:
                    prev_hit, prev_score = acc[cid]
                    acc[cid] = (prev_hit, prev_score + contribution)
                else:
                    acc[cid] = (hit, contribution)
        merged = [
            Hit(chunk=h.chunk, score=s, source=h.source) for h, s in acc.values()
        ]
        merged.sort(key=lambda h: h.score, reverse=True)
        return merged
