"""R4 缓存：精确缓存（默认）+ 语义缓存协议。键含版本，统计命中/未命中供误命中率评测。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from hooks import LLMResponse

__all__ = ["SemanticCache", "ExactCache"]


@runtime_checkable
class SemanticCache(Protocol):
    """缓存协议：get/put/invalidate，键含版本（可插拔契约）。"""

    name: str

    def get(self, key: str, *, version: str = "v1") -> LLMResponse | None:
        """命中返回响应，未命中 None。"""
        ...

    def put(self, key: str, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入响应。"""
        ...

    def invalidate(self, key: str, *, version: str = "v1") -> None:
        """失效某键。"""
        ...


class ExactCache:
    """精确缓存默认实现：内存字典 + (key, version) 二级隔离 + 命中统计。

    语义阈值保守（精确匹配才命中），从根上避免语义误命中。
    """

    name = "exact_cache"

    def __init__(self) -> None:
        """初始化空缓存与统计。"""
        self._store: dict[tuple[str, str], LLMResponse] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str, *, version: str = "v1") -> LLMResponse | None:
        """命中 +1 返回响应；未命中 +1 返回 None。"""
        result = self._store.get((key, version))
        if result is None:
            self.misses += 1
        else:
            self.hits += 1
        return result

    def put(self, key: str, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入（key, version）→ 响应。"""
        self._store[(key, version)] = resp

    def invalidate(self, key: str, *, version: str = "v1") -> None:
        """删除（key, version）条目；不存在时静默。"""
        self._store.pop((key, version), None)
