"""R4-T2 缓存可插拔后端测试（v1.4）：异步协议 + 可换 store + 可换 embedder。

用 fake async store（模拟 Redis）和 fake embedder 验证可插拔性，不引入真实依赖。
"""
from __future__ import annotations

import asyncio

from hooks import LLMRequest, LLMResponse

from harness.model import (
    CacheEmbedder,
    CachePort,
    CacheStore,
    ExactCache,
    InMemoryCacheStore,
    SimilarityCache,
)
from tests.r4.conftest import resp


def _req(text: str) -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}])


class FakeAsyncStore:
    """模拟 Redis 风格异步键值存储（scan 支持前缀遍历）。"""
    name = "fake_async_store"

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def put(self, key: str, value: str, *, ttl: int | None = None) -> None:
        self.data[key] = value
        self.ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def scan(self, prefix: str) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self.data.items() if k.startswith(prefix)]


class FakeEmbedder:
    """假 embedding：按关键词映射固定向量（验证注入路径，非真实模型）。"""
    name = "fake_embedder"

    async def embed(self, text: str) -> tuple[float, ...]:
        if "python" in text.lower():
            return (1.0, 0.0, 0.0)
        if "java" in text.lower():
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)


class TestAsyncProtocol:
    def test_store_protocol_structural(self) -> None:
        assert isinstance(FakeAsyncStore(), CacheStore)
        assert isinstance(InMemoryCacheStore(), CacheStore)

    def test_embedder_protocol_structural(self) -> None:
        assert isinstance(FakeEmbedder(), CacheEmbedder)

    def test_caches_still_are_cache_port(self) -> None:
        assert isinstance(ExactCache(), CachePort)
        assert isinstance(SimilarityCache(), CachePort)

    def test_get_put_are_coroutines(self) -> None:
        """异步协议：get/put/invalidate 返回 coroutine（可 await）。"""
        import inspect
        cache = ExactCache()
        assert inspect.iscoroutinefunction(cache.get)
        assert inspect.iscoroutinefunction(cache.put)
        assert inspect.iscoroutinefunction(cache.invalidate)


class TestExactCacheWithStore:
    def test_uses_injected_store(self) -> None:
        store = FakeAsyncStore()
        cache = ExactCache(store=store)
        asyncio.run(cache.put(_req("hello"), resp("answer")))
        got = asyncio.run(cache.get(_req("hello")))
        assert got is not None and got.content == "answer"
        assert len(store.data) == 1  # 真写进了注入的 store

    def test_roundtrip_via_json(self) -> None:
        """外部 store 存 JSON 字符串，读回还原完整 LLMResponse（含 usage）。"""
        store = FakeAsyncStore()
        cache = ExactCache(store=store)
        r = resp("answer", cost=0.25)
        asyncio.run(cache.put(_req("hello"), r))
        got = asyncio.run(cache.get(_req("hello")))
        assert got == r


class TestSimilarityWithEmbedderAndStore:
    def test_embedder_drives_similarity(self) -> None:
        """注入 embedder：用向量余弦相似度而非文本，语义相近但字面不同也能命中。"""
        store = FakeAsyncStore()
        cache = SimilarityCache(store=store, embedder=FakeEmbedder(), threshold=0.9)
        asyncio.run(cache.put(_req("tell me about python"), resp("Python answer")))
        # 字面差异大，但 embedder 都映射到 python 向量 → 命中
        got = asyncio.run(cache.get(_req("python programming language")))
        assert got is not None and got.content == "Python answer"

    def test_different_embedding_misses(self) -> None:
        cache = SimilarityCache(embedder=FakeEmbedder(), threshold=0.9)
        asyncio.run(cache.put(_req("python"), resp("py")))
        assert asyncio.run(cache.get(_req("java"))) is None  # 向量正交

    def test_without_embedder_uses_text_similarity(self) -> None:
        """不注入 embedder 仍用 difflib 文本相似度（默认开箱即用）。"""
        cache = SimilarityCache(threshold=0.7)
        asyncio.run(cache.put(_req("what is the capital of france"), resp("Paris")))
        assert asyncio.run(cache.get(_req("what is capital of france"))) is not None

    def test_store_records_embeddings(self) -> None:
        """注入 store 后向量被持久化（供跨实例/远程复用）。"""
        store = FakeAsyncStore()
        cache = SimilarityCache(store=store, embedder=FakeEmbedder())
        asyncio.run(cache.put(_req("python"), resp("py")))
        assert len(store.data) == 1
