"""R6-T05：空结果确定性降级——返回明确提示，不编造。"""
from __future__ import annotations

import asyncio

from harness.rag.pipeline import RAGPipeline
from harness.rag.post import DefaultPostProcessor
from harness.rag.retriever import VectorRetriever
from harness.rag.types import Query, RAGResult

from .conftest import FakeEmbedder, FakeSplitter, InMemoryVectorStore


def test_empty_result_is_explicit() -> None:
    """无命中时 RAGResult.empty=True 且 context 为固定提示。"""
    store = InMemoryVectorStore()
    p = RAGPipeline(
        FakeSplitter(), FakeEmbedder(), store, VectorRetriever(store),
        DefaultPostProcessor(),
    )
    out = asyncio.run(p.query(Query(text="nothing here", top_k=3)))
    assert isinstance(out, RAGResult)
    assert out.empty is True
    assert out.hits == []
    assert out.context
    assert "No relevant documents" in out.context or "empty" in out.context.lower()
