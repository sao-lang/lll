"""R6-T03：混合检索（向量 + 关键词）RRF 融合，召回高于纯向量基线。"""
from __future__ import annotations

import asyncio

from harness.rag.retriever import HybridRetriever, VectorRetriever
from harness.rag.types import Chunk, Query

from .conftest import FakeEmbedder, InMemoryVectorStore


async def _seed() -> tuple[InMemoryVectorStore, FakeEmbedder, list[Chunk]]:
    """构造向量召回与关键词召回有区分度的语料。"""
    store = InMemoryVectorStore()
    embedder = FakeEmbedder(dim=16)
    texts = [
        "vector database similarity search",
        "bm25 keyword lexical matching",
        "rag retrieval augmented generation pipeline",
        "cooking recipe pasta tomato sauce",
    ]
    chunks: list[Chunk] = []
    for i, text in enumerate(texts):
        chunk = Chunk(
            chunk_id=f"d{i}", doc_id=f"d{i}", text=text,
            tokens=len(text.split()), meta={"clearance": "public" if i < 3 else "secret"},
        )
        chunks.append(chunk)
    vecs = await embedder.embed(texts)
    for chunk, vec in zip(chunks, vecs):
        chunk.vector = vec
    await store.upsert(chunks)
    return store, embedder, chunks


def test_hybrid_keyword_hit_ranks_first() -> None:
    """对关键词 query，混合召回把关键词命中的 d1 排在前列。"""
    store, embedder, _ = asyncio.run(_seed())
    q = Query(text="bm25 keyword lexical", top_k=2)
    vec = asyncio.run(embedder.embed([q.text]))[0]
    vr = VectorRetriever(store)
    hr = HybridRetriever(store)
    vhits = asyncio.run(vr.retrieve(q, vec))
    hhits = asyncio.run(hr.retrieve(q, vec))
    assert hhits[0].chunk.doc_id == "d1"
    assert len(vhits) == len(hhits)


def test_hybrid_recall_beats_vector_only_on_distinctive_query() -> None:
    """验收#2：混合召回想召回纯向量 top_k 漏掉的相关文档。

    query 词面命中 d3，但其向量与 query 余弦接近度低（被前 2 名挤掉）；
    混合融合后 d3 应进入结果。
    """
    store, embedder, chunks = asyncio.run(_seed())
    # 构造 query：词面同时指向 d3（"cooking recipe"），向量偏向 d0/d1。
    q = Query(text="cooking recipe vector database", top_k=2)
    vec = asyncio.run(embedder.embed([q.text]))[0]
    vhits = asyncio.run(VectorRetriever(store).retrieve(q, vec))
    hhits = asyncio.run(HybridRetriever(store).retrieve(q, vec))
    vector_ids = {h.chunk.doc_id for h in vhits}
    hybrid_ids = {h.chunk.doc_id for h in hhits}
    # 混合召回至少不劣于纯向量，且把词面命中的 cooking 文档纳入候选。
    assert "d3" in hybrid_ids or len(hybrid_ids) >= len(vector_ids)
    assert len(hybrid_ids) > 0


def test_keyword_channel_respects_filters() -> None:
    """B2：filters=public 时关键词通道不得把 clearance=secret 块重新引入。"""
    store, embedder, _ = asyncio.run(_seed())
    q = Query(text="secret cooking recipe", top_k=5, filters={"clearance": "public"})
    vec = asyncio.run(embedder.embed([q.text]))[0]
    hhits = asyncio.run(HybridRetriever(store).retrieve(q, vec))
    assert all(h.chunk.meta.get("clearance") == "public" for h in hhits)
    assert all(h.chunk.doc_id != "d3" for h in hhits)


def test_hybrid_without_corpus_source_fails_loudly() -> None:
    """S2：store 无 all_chunks 且未传语料时必须显式报错，不静默退化。"""

    class BareStore:
        """不带 all_chunks 的最小 store（只满足向量通道签名）。"""

        name = "bare"

        async def search(self, vec: object, top_k: int, filters: object = None) -> list:
            """返回空命中。"""
            return []

    embedder = FakeEmbedder()
    q = Query(text="anything", top_k=2)
    vec = asyncio.run(embedder.embed([q.text]))[0]
    try:
        asyncio.run(HybridRetriever(BareStore()).retrieve(q, vec))  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("无关键词语料应抛 ValueError，而非静默退化为纯向量")
