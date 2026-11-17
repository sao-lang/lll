"""R4-T2 缓存测试：精确缓存 + 语义相似度缓存（v1.2 协议改收 LLMRequest）。

协议 CachePort.get/put/invalidate 收 LLMRequest（原文可达），实现者自行决定 key 策略：
- ExactCache：内部 sha256 精确匹配（行为不变）
- SimilarityCache：归一化文本相似度匹配（纯 Python，不依赖 R6 Embedder）
"""
from __future__ import annotations

from hooks import LLMRequest

from harness.model import CachePort, ExactCache, SimilarityCache
from tests.r4.conftest import resp


def _req(text: str) -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}])


class TestExactCache:
    def test_put_get_hit(self) -> None:
        cache = ExactCache()
        r = resp("answer")
        cache.put(_req("hello"), r)
        assert cache.get(_req("hello")) == r

    def test_miss_returns_none(self) -> None:
        cache = ExactCache()
        assert cache.get(_req("absent")) is None

    def test_different_text_misses(self) -> None:
        cache = ExactCache()
        cache.put(_req("hello"), resp("x"))
        assert cache.get(_req("hello world")) is None  # 精确匹配：多一个词就未命中

    def test_invalidate(self) -> None:
        cache = ExactCache()
        cache.put(_req("k1"), resp("x"))
        cache.invalidate(_req("k1"))
        assert cache.get(_req("k1")) is None

    def test_stats(self) -> None:
        cache = ExactCache()
        cache.put(_req("k"), resp("x"))
        cache.get(_req("k"))      # hit
        cache.get(_req("nope"))   # miss
        assert cache.hits == 1 and cache.misses == 1

    def test_version_scoped(self) -> None:
        cache = ExactCache()
        cache.put(_req("k"), resp("v1-answer"), version="v1")
        assert cache.get(_req("k"), version="v1") is not None
        assert cache.get(_req("k"), version="v2") is None

    def test_implements_protocol(self) -> None:
        assert isinstance(ExactCache(), CachePort)


class TestSimilarityCache:
    def test_exact_text_hits(self) -> None:
        cache = SimilarityCache()
        r = resp("semantic answer")
        cache.put(_req("what is python"), r)
        assert cache.get(_req("what is python")) == r

    def test_near_text_hits(self) -> None:
        """高相似文本命中（语义缓存的核心能力，ExactCache 做不到）。"""
        cache = SimilarityCache(threshold=0.7)
        cache.put(_req("what is the capital of france"), resp("Paris"))
        assert cache.get(_req("what is capital of france")) is not None  # 仅差一个词

    def test_distant_text_misses(self) -> None:
        """低相似文本不命中（保守阈值，避免误命中）。"""
        cache = SimilarityCache(threshold=0.9)
        cache.put(_req("what is python"), resp("a language"))
        assert cache.get(_req("how to bake bread")) is None

    def test_version_scoped(self) -> None:
        cache = SimilarityCache()
        cache.put(_req("hello"), resp("v1"), version="v1")
        assert cache.get(_req("hello"), version="v1") is not None
        assert cache.get(_req("hello"), version="v2") is None

    def test_invalidate(self) -> None:
        cache = SimilarityCache()
        cache.put(_req("hello"), resp("x"))
        cache.invalidate(_req("hello"))
        assert cache.get(_req("hello")) is None

    def test_implements_protocol(self) -> None:
        assert isinstance(SimilarityCache(), CachePort)

    def test_works_without_rag_package(self) -> None:
        """语义缓存用纯 Python 相似度：实例化+命中不依赖 R6/向量库即可工作。"""
        # 只 import 标准库 difflib + hooks/harness；若偷偷依赖 rag 包，本测试环境未装会直接失败
        cache = SimilarityCache(threshold=0.7)
        cache.put(_req("query about python"), resp("answer"))
        assert cache.get(_req("query about python")) is not None
