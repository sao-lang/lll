"""R6-T01/T01b/T02：五段可替换、RAGPort 履约、入库→检索闭环。"""
from __future__ import annotations

import asyncio

from hooks import PORT_PROTOCOLS

from harness.rag.pipeline import RAGPipeline
from harness.rag.post import DefaultPostProcessor
from harness.rag.retriever import VectorRetriever
from harness.rag.types import Query, RAGResult

from .conftest import FakeEmbedder, FakeSplitter, InMemoryVectorStore, doc, pctx


def make_pipeline() -> RAGPipeline:
    """用默认组件构造流水线（重写/重排/缓存关闭以隔离基础行为）。"""
    store = InMemoryVectorStore()
    return RAGPipeline(
        splitter=FakeSplitter(size=6),
        embedder=FakeEmbedder(dim=8),
        store=store,
        retriever=VectorRetriever(store),
        post=DefaultPostProcessor(),
    )


def test_ingest_then_query_returns_ragresult() -> None:
    """R6-T02：入库两文档后查询，返回结构正确的 RAGResult。"""
    p = make_pipeline()

    async def run() -> RAGResult:
        await p.ingest(
            [doc("d1", "alpha beta gamma delta"), doc("d2", "machine learning rag")],
            pctx(),
        )
        return await p.query(Query(text="alpha beta", top_k=2))

    out = asyncio.run(run())
    assert isinstance(out, RAGResult)
    assert out.hits
    assert out.context
    assert out.citations


def test_ragport_retrieve_signature_and_isinstance() -> None:
    """R6-T01b：履约 RAGPort，isinstance 校验过，retrieve 返回带引用的 DynamicValue。"""
    p = make_pipeline()
    assert isinstance(p, PORT_PROTOCOLS["rag"])
    assert p.name == "rag_pipeline"

    async def run() -> list:
        await p.ingest([doc("d1", "alpha beta gamma")], pctx())
        return await p.retrieve("alpha", pctx())

    hits = asyncio.run(run())
    assert isinstance(hits, list)
    assert all(isinstance(h, dict) for h in hits)
    assert "citation" in hits[0]


def test_with_replacement_is_immutable() -> None:
    """R6-T01：with_ 不可变替换——返回新实例，原流水线分段未变。"""
    p = make_pipeline()
    replacement = DefaultPostProcessor()
    q = p.with_(post=replacement)
    assert q is not p
    assert p.post is not q.post
    assert q.post is replacement


def test_ingest_returns_chunk_count() -> None:
    """RAGPort.ingest 返回成功入库的块数（int）。"""
    p = make_pipeline()

    async def run() -> int:
        return await p.ingest([doc("d1", "abcdefghijkl")], pctx())  # 12 字 / size 6 = 2 块

    assert asyncio.run(run()) == 2
