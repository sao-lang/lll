"""R4-T2 缓存测试：精确缓存 + 语义相似度缓存。

协议 CachePort.get/put/invalidate 是 async，收 LLMRequest 原文：
- ExactCache：内部 sha256 精确匹配
- SimilarityCache：文本/向量相似度匹配（不依赖 R6 时走 difflib）
"""
from __future__ import annotations

import asyncio

from hooks import LLMRequest

from harness.model import CachePort, ExactCache, SimilarityCache
from tests.r4.conftest import resp


def _req(text: str) -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}])


class TestExactCache:
    def test_put_get_hit(self) -> None:
        cache = ExactCache()
        r = resp("answer")
        asyncio.run(cache.put(_req("hello"), r))
        assert asyncio.run(cache.get(_req("hello"))) == r

    def test_miss_returns_none(self) -> None:
        cache = ExactCache()
        assert asyncio.run(cache.get(_req("absent"))) is None

    def test_different_text_misses(self) -> None:
        cache = ExactCache()
        asyncio.run(cache.put(_req("hello"), resp("x")))
        assert asyncio.run(cache.get(_req("hello world"))) is None

    def test_invalidate(self) -> None:
        cache = ExactCache()
        asyncio.run(cache.put(_req("k1"), resp("x")))
        asyncio.run(cache.invalidate(_req("k1")))
        assert asyncio.run(cache.get(_req("k1"))) is None

    def test_stats(self) -> None:
        cache = ExactCache()
        asyncio.run(cache.put(_req("k"), resp("x")))
        asyncio.run(cache.get(_req("k")))      # hit
        asyncio.run(cache.get(_req("nope")))   # miss
        assert cache.hits == 1 and cache.misses == 1

    def test_version_scoped(self) -> None:
        cache = ExactCache()
        asyncio.run(cache.put(_req("k"), resp("answer-1"), version="current"))
        assert asyncio.run(cache.get(_req("k"), version="current")) is not None
        assert asyncio.run(cache.get(_req("k"), version="v2")) is None

    def test_implements_protocol(self) -> None:
        assert isinstance(ExactCache(), CachePort)


class TestSimilarityCache:
    def test_exact_text_hits(self) -> None:
        cache = SimilarityCache()
        r = resp("semantic answer")
        asyncio.run(cache.put(_req("what is python"), r))
        assert asyncio.run(cache.get(_req("what is python"))) == r

    def test_near_text_hits(self) -> None:
        cache = SimilarityCache(threshold=0.7)
        asyncio.run(cache.put(_req("what is the capital of france"), resp("Paris")))
        assert asyncio.run(cache.get(_req("what is capital of france"))) is not None

    def test_distant_text_misses(self) -> None:
        cache = SimilarityCache(threshold=0.9)
        asyncio.run(cache.put(_req("what is python"), resp("a language")))
        assert asyncio.run(cache.get(_req("how to bake bread"))) is None

    def test_version_scoped(self) -> None:
        cache = SimilarityCache()
        asyncio.run(cache.put(_req("hello"), resp("answer-2"), version="current"))
        assert asyncio.run(cache.get(_req("hello"), version="current")) is not None
        assert asyncio.run(cache.get(_req("hello"), version="v2")) is None

    def test_invalidate(self) -> None:
        cache = SimilarityCache()
        asyncio.run(cache.put(_req("hello"), resp("x")))
        asyncio.run(cache.invalidate(_req("hello")))
        assert asyncio.run(cache.get(_req("hello"))) is None

    def test_implements_protocol(self) -> None:
        assert isinstance(SimilarityCache(), CachePort)

    def test_works_without_rag_package(self) -> None:
        cache = SimilarityCache(threshold=0.7)
        asyncio.run(cache.put(_req("query about python"), resp("answer")))
        assert asyncio.run(cache.get(_req("query about python"))) is not None
