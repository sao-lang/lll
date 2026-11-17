"""R4-T2 语义/精确缓存测试：命中返回 / 键含版本 / 失效 / 不污染原响应。"""
from __future__ import annotations

from harness.model import ExactCache, SemanticCache
from tests.r4.conftest import resp


class TestExactCache:
    def test_put_get_hit(self) -> None:
        """写入后命中返回同一响应。"""
        cache = ExactCache()
        r = resp("answer")
        cache.put("k1", r)
        assert cache.get("k1") == r

    def test_miss_returns_none(self) -> None:
        cache = ExactCache()
        assert cache.get("absent") is None

    def test_invalidate(self) -> None:
        cache = ExactCache()
        cache.put("k1", resp("x"))
        cache.invalidate("k1")
        assert cache.get("k1") is None

    def test_stats(self) -> None:
        """命中/未命中有统计（供评测误命中率）。"""
        cache = ExactCache()
        cache.put("k1", resp("x"))
        cache.get("k1")  # hit
        cache.get("nope")  # miss
        assert cache.hits == 1 and cache.misses == 1

    def test_version_scoped(self) -> None:
        """get/put 带 version 参数：跨版本隔离。"""
        cache = ExactCache()
        cache.put("k", resp("v1-answer"), version="v1")
        assert cache.get("k", version="v1") is not None
        assert cache.get("k", version="v2") is None

    def test_implements_protocol(self) -> None:
        assert isinstance(ExactCache(), SemanticCache)
