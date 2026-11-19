"""R6-T07：语义缓存——命中走缓存、invalidate 后失效。"""
from __future__ import annotations

import asyncio

from harness.rag.pipeline import RAGPipeline
from harness.rag.post import DefaultPostProcessor
from harness.rag.retriever import VectorRetriever
from harness.rag.types import Query

from .conftest import (
    CountingEmbedder,
    CountingRetriever,
    FakeSplitter,
    InMemoryVectorStore,
    doc,
    pctx,
)


def test_cache_hit_skips_retrieval_and_invalidate() -> None:
    """相同 query 第二次命中缓存（不重复召回），invalidate 后重算。"""
    store = InMemoryVectorStore()
    retriever = CountingRetriever(VectorRetriever(store))
    p = RAGPipeline(
        FakeSplitter(), CountingEmbedder(), store, retriever,
        DefaultPostProcessor(), cache=True,
    )

    async def run() -> tuple[int, object, object]:
        await p.ingest([doc("d1", "cache me please")], pctx())
        first = await p.query(Query(text="cache me", top_k=2))
        n_after_first = retriever.calls
        second = await p.query(Query(text="cache me", top_k=2))
        mid = retriever.calls
        await p.invalidate_cache()
        await p.query(Query(text="cache me", top_k=2))
        return n_after_first, mid, (second.context == first.context)

    n_first, mid, same = asyncio.run(run())
    assert mid == n_first  # 命中缓存，未再召回
    assert same is True
    assert retriever.calls == n_first + 1  # invalidate 后重算
