"""R6 缓存按 key 失效回归：ingest 只失效本实例写过的 key，不清空整表。"""
from __future__ import annotations

import asyncio

from harness.rag import DefaultPostProcessor, VectorRetriever
from harness.rag.pipeline import CachePort, RAGPipeline
from harness.rag.types import Query, RAGResult

from .conftest import CountingEmbedder, FakeSplitter, InMemoryVectorStore, doc, pctx


class _SpyCache:
    """记录 invalidate 调用参数的 CachePort 替身，验证按-key 路径。"""

    name = "spy_cache"

    def __init__(self) -> None:
        """初始化空数据与调用记录。"""
        self.data: dict[str, RAGResult] = {}
        self.invalidate_calls: list[str | None] = []

    def get(self, key: str) -> RAGResult | None:
        """按键读取。"""
        return self.data.get(key)

    def set(self, key: str, value: RAGResult) -> None:
        """按键写入。"""
        self.data[key] = value

    def invalidate(self, key: str | None = None) -> None:
        """记录调用参数后失效单键；None 表示清空。"""
        self.invalidate_calls.append(key)
        if key is None:
            self.data.clear()
        else:
            self.data.pop(key, None)


def _pipeline(cache: CachePort) -> RAGPipeline:
    """构造注入 spy 缓存的流水线。"""
    store = InMemoryVectorStore()
    return RAGPipeline(
        FakeSplitter(), CountingEmbedder(), store,
        VectorRetriever(store), DefaultPostProcessor(), cache=cache,
    )


def test_ingest_invalidates_by_key_not_whole_cache() -> None:
    """ingest 后按具体 key 逐个失效，不触发无参全清；账本清空。"""
    cache = _SpyCache()
    p = _pipeline(cache)

    async def run() -> tuple[list[str | None], set[str], int]:
        await p.ingest([doc("d1", "alpha beta")], pctx())
        await p.query(Query(text="alpha", top_k=2))
        await p.query(Query(text="beta", top_k=3))
        keys_before = set(p._cache_keys)
        await p.ingest([doc("d2", "gamma delta")], pctx())
        return list(cache.invalidate_calls), keys_before, len(p._cache_keys)

    calls, keys_before, ledger_after = asyncio.run(run())
    assert len(keys_before) == 2, "两个不同 query 各登记一个 key"
    assert calls == list(keys_before) or set(calls) == keys_before, "invalidate 收到具体 key"
    assert all(k is not None for k in calls), "禁止无参 invalidate() 全表清空"
    assert ledger_after == 0, "失效后账本清空"
    assert cache.data == {}, "两个 key 都被移除"


def test_per_instance_ledger_does_not_touch_other_instances_keys() -> None:
    """A1：两个独立 pipeline 共享 cache，B ingest 只删自己的 key，不误删 A 的。"""
    cache = _SpyCache()
    a = _pipeline(cache)
    b = _pipeline(cache)

    async def run() -> tuple[set[str], set[str], set[str]]:
        await a.ingest([doc("d1", "alpha beta")], pctx())
        await a.query(Query(text="alpha", top_k=2))
        key_a = set(a._cache_keys)
        await b.ingest([doc("d2", "gamma delta")], pctx())
        await b.query(Query(text="gamma", top_k=2))
        key_b = set(b._cache_keys)
        before = set(cache.data)
        await b.ingest([doc("d3", "epsilon")], pctx())
        return key_a, key_b, before

    key_a, key_b, before = asyncio.run(run())
    assert key_a.isdisjoint(key_b), "两实例登记不同 key"
    assert before == key_a | key_b
    assert key_a <= set(cache.data), "A 的 key 不被 B 的 ingest 误删"
    assert not (key_b & set(cache.data)), "B 的 key 被删"
    assert None not in cache.invalidate_calls, "全程无无参全清"


def test_with_shares_or_resets_ledger() -> None:
    """with_ 共享 cache → 共享账本；换 cache → 新实例空账本。"""
    cache = _SpyCache()
    p = _pipeline(cache)

    async def seed() -> None:
        await p.ingest([doc("d1", "alpha beta")], pctx())
        await p.query(Query(text="alpha", top_k=2))

    asyncio.run(seed())
    q_same = p.with_(rewrite=None)
    assert q_same._cache_keys is p._cache_keys, "同 cache 实例共享账本对象"
    assert q_same._cache is p._cache

    async def cross_invalidate() -> set[str]:
        await q_same.query(Query(text="beta", top_k=2))
        both = set(p._cache_keys)
        await q_same.ingest([doc("d2", "gamma")], pctx())
        return both

    both = asyncio.run(cross_invalidate())
    assert len(both) == 2, "两侧写入的 key 都在共享账本"
    assert cache.data == {}, "新实例 ingest 失效了两侧写入的 key（A2 行为级）"

    other = _SpyCache()
    q_other = p.with_(cache=other)
    assert q_other._cache is other
    assert q_other._cache_keys == set(), "换 cache 后新实例账本为空"


def test_cache_disabled_and_empty_ingest() -> None:
    """Nit-2/3：cache 关闭时账本空且无失效；空 chunks 不触发失效。"""
    store = InMemoryVectorStore()
    p_off = RAGPipeline(
        FakeSplitter(), CountingEmbedder(), store,
        VectorRetriever(store), DefaultPostProcessor(), cache=False,
    )
    assert p_off._cache_keys == set()
    assert asyncio.run(p_off.ingest([doc("d", "some text")], pctx())) > 0
    assert p_off._cache_keys == set(), "关闭缓存不登记 key"

    cache = _SpyCache()
    p = _pipeline(cache)

    async def run() -> tuple[int, list[str | None]]:
        n = await p.ingest([doc("empty", "")], pctx())  # 空文本切不出 chunk
        await p.query(Query(text="x", top_k=2))
        before = len(cache.invalidate_calls)
        await p.ingest([], pctx())  # 空文档列表：提前返回
        return n, cache.invalidate_calls[before:]

    n, new_calls = asyncio.run(run())
    assert n == 0, "空文本切不出 chunk，返回 0"
    assert new_calls == [], "空 ingest 不触发失效（既有语义：无新文档入库）"
