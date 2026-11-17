"""R4 缓存：可插拔缓存协议 + 两个默认实现（精确 / 语义相似度）。

v1.2 协议重构：get/put/invalidate 从收 `key: str`（不可逆 sha256）改为收 `LLMRequest`（原文），
使语义缓存实现拿得到 query 原文、真正可实现相似度匹配。key 策略下沉到各实现内部。
- CachePort：缓存协议（名实一致，中性命名；旧名 SemanticCache 保留为别名兼容）
- ExactCache：默认实现，内部 sha256 精确匹配（行为与 v1.1 完全一致）
- SimilarityCache：默认实现，归一化文本相似度（纯 Python，不依赖 R6 Embedder，开箱即用）
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Protocol, runtime_checkable

from hooks import LLMRequest, LLMResponse

from .types import build_cache_key

__all__ = ["CachePort", "ExactCache", "SimilarityCache", "SemanticCache"]


@runtime_checkable
class CachePort(Protocol):
    """缓存协议：get/put/invalidate，收 LLMRequest 原文，键版本由实现内部隔离。

    为何收 request 而非 key（v1.2 重构）：
        旧签名收 `key: str`（网关侧 sha256 后的哈希），语义缓存拿不到原文、无法做相似度——
        "语义缓存"名存实亡。改收 request 后，key 策略归实现者：
        ExactCache 内部仍算精确哈希，SimilarityCache 用文本相似度，用户可实现任意策略。
    """

    name: str

    def get(self, request: LLMRequest, *, version: str = "v1") -> LLMResponse | None:
        """命中返回响应，未命中 None。实现自定匹配策略（精确/相似度/向量等）。"""
        ...

    def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入响应。"""
        ...

    def invalidate(self, request: LLMRequest, *, version: str = "v1") -> None:
        """失效该请求对应的缓存条目。"""
        ...


# 向后兼容别名：旧代码 `from harness.model import SemanticCache` 仍可用；新代码用 CachePort。
SemanticCache = CachePort


class ExactCache:
    """精确缓存默认实现：内部用 build_cache_key 做 sha256 精确匹配。

    键含版本（二级隔离）+ 命中统计。语义阈值保守（精确匹配才命中），从根上避免语义误命中。
    """

    name = "exact_cache"

    def __init__(self) -> None:
        """初始化空缓存与统计。"""
        self._store: dict[tuple[str, str], LLMResponse] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, request: LLMRequest, version: str) -> tuple[str, str]:
        """由 request 算精确键（sha256），与版本组成二级隔离键。"""
        return (build_cache_key(request, prompt_version=version).value, version)

    def get(self, request: LLMRequest, *, version: str = "v1") -> LLMResponse | None:
        """命中 +1 返回响应；未命中 +1 返回 None。"""
        result = self._store.get(self._key(request, version))
        if result is None:
            self.misses += 1
        else:
            self.hits += 1
        return result

    def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入 (精确键, 版本) → 响应。"""
        self._store[self._key(request, version)] = resp

    def invalidate(self, request: LLMRequest, *, version: str = "v1") -> None:
        """删除 (精确键, 版本) 条目；不存在时静默。"""
        self._store.pop(self._key(request, version), None)


def _normalize(text: str) -> str:
    """归一化文本：小写 + 折叠空白，供相似度比较（保守，不丢词序）。"""
    return " ".join(text.lower().split())


# role 有限取值常量（规则 7：避免裸字符串比较；与 R1 Message.role 取值一致）
_ROLE_KEY = "role"
_CONTENT_KEY = "content"
_USER_ROLE = "user"


def _query_text(request: LLMRequest) -> str:
    """从请求提取用于相似度匹配的文本（取最后一条 user 消息，无则拼接全部 content）。"""
    user_texts = [
        str(m.get(_CONTENT_KEY, ""))
        for m in request.messages
        if isinstance(m, dict) and m.get(_ROLE_KEY) == _USER_ROLE
    ]
    if user_texts:
        return _normalize(user_texts[-1])
    all_texts = [
        str(m.get("content", "")) for m in request.messages if isinstance(m, dict)
    ]
    return _normalize(" ".join(all_texts))


class SimilarityCache:
    """语义相似度缓存默认实现：归一化文本的 SequenceMatcher 相似度匹配。

    纯 Python 标准库（difflib），不依赖 R6 Embedder/向量库——开箱即用。
    保守阈值（默认 0.85）避免误命中；语义匹配不足时用户可换向量实现（同为 CachePort）。
    """

    name = "similarity_cache"

    def __init__(self, *, threshold: float = 0.85) -> None:
        """装配相似度阈值。

        参数：
            threshold: 命中最低相似度（0~1）。越高越保守；缺省 0.85。
        """
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self._threshold = threshold
        # version → [(归一化 query, response), ...]
        self._store: dict[str, list[tuple[str, LLMResponse]]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, request: LLMRequest, *, version: str = "v1") -> LLMResponse | None:
        """按相似度找最匹配条目；相似度 ≥ threshold 才命中。"""
        query = _query_text(request)
        best: LLMResponse | None = None
        best_score = 0.0
        for cached_query, resp in self._store.get(version, []):
            score = SequenceMatcher(None, query, cached_query).ratio()
            if score > best_score:
                best_score, best = score, resp
        if best is not None and best_score >= self._threshold:
            self.hits += 1
            return best
        self.misses += 1
        return None

    def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入 (归一化 query, 响应) 到版本分区；同 query 覆盖。"""
        query = _query_text(request)
        entries = self._store.setdefault(version, [])
        for i, (cached_query, _) in enumerate(entries):
            if cached_query == query:
                entries[i] = (query, resp)
                return
        entries.append((query, resp))

    def invalidate(self, request: LLMRequest, *, version: str = "v1") -> None:
        """删除相似度 ≥ threshold 的所有条目（语义失效）。"""
        query = _query_text(request)
        entries = self._store.get(version, [])
        self._store[version] = [
            (q, r) for q, r in entries
            if SequenceMatcher(None, query, q).ratio() < self._threshold
        ]
