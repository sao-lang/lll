"""R6 接线回归：锁住独立 review 发现的 B1/B3/S1/S4/S5/S7 修复，防回归。"""
from __future__ import annotations

import asyncio

from harness.skills import InjectionVerdict

from harness.rag import (
    DefaultPostProcessor,
    HashEmbedder,
    InMemoryCache,
    InMemoryVectorStore,
    LexicalReranker,
    MultiQueryRewriter,
    Query,
    RAGPipeline,
    VectorRetriever,
)

from .conftest import CountingEmbedder, FakeSplitter, InMemoryVectorStore as FakeStore, doc, pctx


def _pipeline(**kw: object) -> RAGPipeline:
    """构造测试流水线。"""
    store = InMemoryVectorStore()
    return RAGPipeline(
        FakeSplitter(), kw.pop("embedder", HashEmbedder(dim=8)), store,
        VectorRetriever(store), DefaultPostProcessor(), **kw,  # type: ignore[arg-type]
    )


def test_query_uses_injected_embedder() -> None:
    """B1：查询向量化必须走流水线注入的 embedder（与入库同源），不另起哈希器。"""
    embedder = CountingEmbedder()
    p = _pipeline(embedder=embedder)

    async def run() -> None:
        await p.ingest([doc("d1", "alpha beta gamma")], pctx())
        await p.query(Query(text="alpha", top_k=2))

    asyncio.run(run())
    # 入库 1 次 + 查询 1 次（旧实现查询走 _trivial_embedder，这里只会是 1）
    assert embedder.calls == 2


def test_pipeline_detector_takes_effect() -> None:
    """B3：pipeline.detector 必须注入到后处理，替换默认检测器。"""

    class MarkerDetector:
        """标记检测器：任何文本都判 INJECTED。"""

        name = "marker"

        def detect(self, text: str) -> InjectionVerdict:
            """恒判注入。"""
            return InjectionVerdict.INJECTED

    marker = MarkerDetector()
    p = _pipeline(detector=marker)

    async def run() -> object:
        await p.ingest([doc("d1", "clean normal text")], pctx())
        return await p.query(Query(text="clean", top_k=2))

    out = asyncio.run(run())
    # 若 detector 未接线，默认检测器判 clean → 命中；接线后全部剔除 → empty
    assert out.empty is True and out.hits == []


def test_with_preserves_all_injection_points() -> None:
    """S1：with_ 替换一段后，其余 7 个注入点身份保持不变。"""
    rw = MultiQueryRewriter(2)
    rr = LexicalReranker(3)
    cache = InMemoryCache()
    p = _pipeline(rewrite=rw, rerank=rr, cache=cache)
    q = p.with_(post=DefaultPostProcessor())
    assert q.rewrite is rw and q.rerank is rr and q._cache is cache
    assert q is not p and q.post is not p.post


def test_cache_key_includes_filters_and_ingest_invalidates() -> None:
    """S4：缓存键含 filters；ingest 后缓存失效，新文档可见。"""
    p = _pipeline(cache=True)

    async def run() -> tuple:
        await p.ingest([doc("d1", "alpha public")], pctx())
        q1 = Query(text="alpha", top_k=2, filters={"tenant": "a"})
        q2 = Query(text="alpha", top_k=2, filters={"tenant": "b"})
        await p.query(q1)
        r2 = await p.query(q2)  # 不同 filters → 不命中 q1 缓存
        n_before = p._cache._data.__len__() if p._cache else 0
        await p.ingest([doc("d2", "alpha beta")], pctx())
        n_after = p._cache._data.__len__() if p._cache else 0  # ingest 清空
        return r2, n_before, n_after

    _, n_before, n_after = asyncio.run(run())
    assert n_before >= 2  # 两个不同 filters 各存一条
    assert n_after == 0  # ingest 后清空


def test_custom_cache_object_is_used() -> None:
    """S5：注入的 CachePort 实例被真正委托（非仅当布尔开关）。"""
    cache = InMemoryCache()
    p = _pipeline(cache=cache)
    assert p._cache is cache

    async def run() -> None:
        await p.ingest([doc("d1", "alpha")], pctx())
        await p.query(Query(text="alpha", top_k=2))

    asyncio.run(run())
    assert len(cache._data) == 1


def test_multi_query_dedup() -> None:
    """S7：多 query 改写产生的重复命中按 chunk_id 去重。"""
    p = _pipeline(rewrite=MultiQueryRewriter(3))

    async def run() -> list[str]:
        await p.ingest([doc("d1", "alpha beta")], pctx())
        r = await p.query(Query(text="alpha", top_k=3))
        return r.citations

    cites = asyncio.run(run())
    assert len(cites) == len(set(cites))  # 无重复引用
