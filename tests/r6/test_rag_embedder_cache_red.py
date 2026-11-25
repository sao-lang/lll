"""RED：RAG 检索缓存键必须含 embedder 身份，换 embedder 不得命中旧结果。

独立审计 EXP5-A 证据：p1.with_(embedder=eB) 共享 _cache 与 _cache_keys，
eB 向量与 eA 正交却仍拿到 A 的缓存结果（score=1.0，且不再 embed）。
VectorRetriever 对正交向量返回 score=0.0 的命中，故用精确 score 与 embed 次数
区分「复用旧缓存」与「按新向量空间重算」。
"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.rag.embedder import Embedder
from harness.rag.pipeline import RAGPipeline
from harness.rag.post import DefaultPostProcessor
from harness.rag.retriever import VectorRetriever
from harness.rag.splitter import RecursiveSplitter
from harness.rag.store import InMemoryVectorStore
from harness.rag.types import Document, Query, Vector


class _FixedEmbedder(Embedder):
    name = "fixed"

    def __init__(self, vec: Vector) -> None:
        self.vec = vec
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[Vector]:
        self.calls += 1
        return [self.vec for _ in texts]


def _pipe(embedder: Embedder, cache: bool) -> RAGPipeline:
    store = InMemoryVectorStore()
    return RAGPipeline(
        RecursiveSplitter(), embedder, store,
        VectorRetriever(store), DefaultPostProcessor(), cache=cache,
    )


def test_red_with_embedder_does_not_reuse_old_cached_hits() -> None:
    async def run() -> None:
        e_a = _FixedEmbedder((1.0, 0.0))
        e_b = _FixedEmbedder((0.0, 1.0))
        p1 = _pipe(e_a, True)
        await p1.ingest([Document(doc_id="d1", text="alpha beta gamma")], PortCtx())
        q = Query(text="alpha", top_k=5)
        first = await p1.query(q)
        assert len(first.hits) == 1
        assert first.hits[0].score == 1.0, "A 空间查询向量与入库向量同向，score 应为 1.0"

        p2 = p1.with_(embedder=e_b)
        assert p1._cache is p2._cache, "前置条件：共享缓存实例"

        second = await p2.query(q)
        assert e_b.calls == 1, (
            f"换 embedder 后必须重新 embed 查询（证明确实没吃 A 的缓存），实际 {e_b.calls}"
        )
        assert len(second.hits) == 1
        assert second.hits[0].score == 0.0, (
            f"eB 与 eA 正交，重算命中 score 应为 0.0（而非 A 缓存的 1.0），"
            f"实际 {second.hits[0].score}"
        )

    asyncio.run(run())
