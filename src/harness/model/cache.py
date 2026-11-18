"""R4 缓存：异步协议 + 可插拔存储工具 + 可插拔 embedding（v1.4）。

三层可插拔（每层都有协议 + 默认实现，符合可插拔四要素）：
1. CachePort：缓存策略协议（异步 get/put/invalidate，收 LLMRequest 原文）
   - ExactCache：精确匹配默认
   - SimilarityCache：语义相似度默认
2. CacheStore：存储工具协议（get/put/delete/scan，存 JSON 字符串）
   - InMemoryCacheStore：进程内默认
   - 用户可实现 RedisCacheStore / DBCacheStore（异步客户端直接可用）
3. CacheEmbedder：embedding 模型协议（embed）
   - 无默认 embedder：SimilarityCache 缺省用 difflib 文本相似度（开箱即用）
   - 用户可注入 OpenAI/本地 embedding 实现，走向量余弦相似度

版本沿革：v1.2 协议收 request 原文（语义缓存可达）；v1.4 协议异步化 + store/embedder 可插拔。
"""
from __future__ import annotations

import json
import math
from difflib import SequenceMatcher
from typing import Protocol, TypedDict, cast, runtime_checkable

from hooks import LLMRequest, LLMResponse

from .types import build_cache_key

__all__ = [
    "CacheEmbedder",
    "CachePort",
    "CacheStore",
    "ExactCache",
    "InMemoryCacheStore",
    "SimilarityCache",
]


# --------------------------------------------------------------------------- #
# 层 1：缓存策略协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class CachePort(Protocol):
    """缓存协议（异步）：get/put/invalidate 收 LLMRequest 原文，键与匹配策略由实现决定。

    异步理由：gateway 的 complete/stream 均 async，Redis(asyncio)/DB(asyncpg) 客户端也是 async；
    同步协议会让远程存储实现者被迫阻塞事件循环。key 策略归实现者：
    ExactCache 内部算精确哈希，SimilarityCache 用文本/向量相似度，用户可实现任意策略。
    """

    name: str

    async def get(self, request: LLMRequest, *, version: str = "v1") -> LLMResponse | None:
        """命中返回响应，未命中 None。"""
        ...

    async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入响应。"""
        ...

    async def invalidate(self, request: LLMRequest, *, version: str = "v1") -> None:
        """失效该请求对应的缓存条目。"""
        ...



# --------------------------------------------------------------------------- #
# 层 2：存储工具协议（用户可换 Redis/DB）
# --------------------------------------------------------------------------- #
@runtime_checkable
class CacheStore(Protocol):
    """缓存存储工具协议（异步）：存取 JSON 字符串，按前缀遍历供语义匹配。

    为何存 str 而非 LLMResponse：外部存储（Redis/DB）只认字节/字符串；
    JSON 序列化在缓存层完成，存储工具保持简单通用。
    """

    name: str

    async def get(self, key: str) -> str | None:
        """读 key → JSON 字符串；不存在 None。"""
        ...

    async def put(self, key: str, value: str, *, ttl: int | None = None) -> None:
        """写 key → JSON 字符串；ttl 秒（None 表示永不过期）。"""
        ...

    async def delete(self, key: str) -> None:
        """删除 key；不存在静默。"""
        ...

    async def scan(self, prefix: str) -> list[tuple[str, str]]:
        """按前缀遍历返回 (key, value) 列表（语义缓存匹配候选集用）。"""
        ...


class InMemoryCacheStore:
    """默认存储工具：进程内字典 + 可选 TTL（墙钟由 time.time 提供，仅测试可注入 clock）。"""

    name = "in_memory_cache_store"

    def __init__(self) -> None:
        """初始化空存储。"""
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}

    async def get(self, key: str) -> str | None:
        """读 key；TTL 过期则删除并返回 None。"""
        import time
        exp = self._expiry.get(key)
        if exp is not None and time.time() >= exp:
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return None
        return self._data.get(key)

    async def put(self, key: str, value: str, *, ttl: int | None = None) -> None:
        """写 key；ttl 给定时设置过期时刻。"""
        import time
        self._data[key] = value
        if ttl is None:
            self._expiry.pop(key, None)
        else:
            self._expiry[key] = time.time() + ttl

    async def delete(self, key: str) -> None:
        """删除 key 及其 TTL。"""
        self._data.pop(key, None)
        self._expiry.pop(key, None)

    async def scan(self, prefix: str) -> list[tuple[str, str]]:
        """返回所有 prefix 开头的 (key, value)（跳过已过期项）。"""
        import time
        now = time.time()
        return [
            (k, v) for k, v in self._data.items()
            if k.startswith(prefix)
            and not (k in self._expiry and now >= self._expiry[k])
        ]


# --------------------------------------------------------------------------- #
# 层 3：embedding 协议（用户可换向量模型）
# --------------------------------------------------------------------------- #
@runtime_checkable
class CacheEmbedder(Protocol):
    """Embedding 模型协议：把文本映射为向量（供语义缓存算余弦相似度）。"""

    name: str

    async def embed(self, text: str) -> tuple[float, ...]:
        """文本 → 向量（维度由实现决定，同一实现内须一致）。"""
        ...


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """余弦相似度；维度不符或零向量返回 0.0（保守，不误命中）。"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------- #
# 共享助手
# --------------------------------------------------------------------------- #
_ROLE_KEY, _CONTENT_KEY, _USER_ROLE = "role", "content", "user"


class _SemanticCacheEntry(TypedDict):
    """语义缓存条目的持久化结构（规则 4：明确字段显式声明）。

    text: 归一化后的查询原文。vec: 可选 embedding 向量（无 embedder 时为 None）。
    resp: LLMResponse 的 JSON 字符串（跨存储工具只认字符串，序列化在缓存层完成）。
    """

    text: str
    vec: list[float] | None
    resp: str


def _parse_entry(raw: str) -> _SemanticCacheEntry | None:
    """把存储工具的 JSON 字符串解析为条目视图；非法 JSON 返回 None。"""
    try:
        return cast(_SemanticCacheEntry, json.loads(raw))
    except json.JSONDecodeError:
        return None


def _query_text(request: LLMRequest) -> str:
    """提取用于相似度匹配的文本（最后一条 user 消息，无则拼全部 content）。"""
    user_texts = [
        str(m.get(_CONTENT_KEY, ""))
        for m in request.messages
        if isinstance(m, dict) and m.get(_ROLE_KEY) == _USER_ROLE
    ]
    if user_texts:
        return " ".join(user_texts[-1].lower().split())
    all_texts = [
        str(m.get(_CONTENT_KEY, "")) for m in request.messages if isinstance(m, dict)
    ]
    return " ".join(" ".join(all_texts).lower().split())


def _store_key(kind: str, version: str, identity: str) -> str:
    """存储键：前缀隔离实现类型 + 版本 + 身份（供 scan 按其前缀遍历）。"""
    return f"cache:{kind}:{version}:{identity}"


# --------------------------------------------------------------------------- #
# 默认实现 1：精确缓存
# --------------------------------------------------------------------------- #
class ExactCache:
    """精确缓存：sha256 精确匹配；存储工具可注入（缺省 InMemoryCacheStore）。"""

    name = "exact_cache"

    def __init__(self, *, store: CacheStore | None = None, ttl: int | None = None) -> None:
        """装配精确缓存。

        参数：
            store: 存储工具（缺省内存）；可注入 Redis/DB 实现。
            ttl: 缓存条目生存秒数（None 永久）；经 store.put 传给后端。
        """
        self._store = store if store is not None else InMemoryCacheStore()
        self._ttl = ttl
        self.hits = 0
        self.misses = 0

    def _identity(self, request: LLMRequest, version: str) -> str:
        """精确身份 = request 的 sha256（含版本）。"""
        return build_cache_key(request, prompt_version=version).value

    async def get(self, request: LLMRequest, *, version: str = "v1") -> LLMResponse | None:
        """命中 +1 返回响应；未命中 +1 返回 None。"""
        key = _store_key("exact", version, self._identity(request, version))
        raw = await self._store.get(key)
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return LLMResponse.model_validate_json(raw)

    async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入精确键 → 响应 JSON。"""
        key = _store_key("exact", version, self._identity(request, version))
        await self._store.put(key, resp.model_dump_json(), ttl=self._ttl)

    async def invalidate(self, request: LLMRequest, *, version: str = "v1") -> None:
        """删除精确键。"""
        key = _store_key("exact", version, self._identity(request, version))
        await self._store.delete(key)


# --------------------------------------------------------------------------- #
# 默认实现 2：语义相似度缓存
# --------------------------------------------------------------------------- #
class SimilarityCache:
    """语义相似度缓存：可注入 embedder（向量余弦）与 store，缺省纯 Python 文本相似度。

    不注入 embedder 时用 difflib（开箱即用，零依赖）；注入后走向量相似度，语义更强。
    两种模式都把候选存进 CacheStore（可 Redis/DB），跨实例共享。
    """

    name = "similarity_cache"

    def __init__(
        self,
        *,
        threshold: float = 0.85,
        store: CacheStore | None = None,
        embedder: CacheEmbedder | None = None,
        ttl: int | None = None,
    ) -> None:
        """装配语义缓存。

        参数：
            threshold: 命中最低相似度（0~1），越高越保守。
            store: 存储工具（缺省内存）。embedder: 向量模型（缺省 difflib 文本相似度）。
            ttl: 条目生存秒数。
        """
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self._threshold = threshold
        self._store = store if store is not None else InMemoryCacheStore()
        self._embedder = embedder
        self._ttl = ttl
        self.hits = 0
        self.misses = 0

    def _identity(self, text: str) -> str:
        """身份 = 原文（有 embedder 时用向量串，供确定性键）。"""
        return text

    async def _vector_of(self, text: str) -> tuple[float, ...] | None:
        """有 embedder 才算向量；否则 None（走文本路径）。"""
        return await self._embedder.embed(text) if self._embedder is not None else None

    async def get(self, request: LLMRequest, *, version: str = "v1") -> LLMResponse | None:
        """按相似度找最匹配条目；≥ threshold 才命中。"""
        query = _query_text(request)
        q_vec = await self._vector_of(query)
        # 条目结构见 _SemanticCacheEntry（text/vec/resp 明确字段）。
        prefix = _store_key("semantic", version, "")
        best: LLMResponse | None = None
        best_score = 0.0
        for _key, raw in await self._store.scan(prefix):
            entry = _parse_entry(raw)
            if entry is None:
                continue
            if q_vec is not None and entry["vec"] is not None:
                score = _cosine(q_vec, tuple(entry["vec"]))
            else:
                score = SequenceMatcher(None, query, entry["text"]).ratio()
            if score > best_score:
                best_score = score
                best = LLMResponse.model_validate_json(entry["resp"])
        if best is not None and best_score >= self._threshold:
            self.hits += 1
            return best
        self.misses += 1
        return None

    async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "v1") -> None:
        """写入条目（原文 + 可选向量 + 响应 JSON）到存储工具。"""
        query = _query_text(request)
        vec = await self._vector_of(query)
        entry: _SemanticCacheEntry = {
            "text": query,
            "vec": list(vec) if vec is not None else None,
            "resp": resp.model_dump_json(),
        }
        # 键含原文 hash，保证同 query 覆盖；scan 按前缀取回全部候选
        identity = build_cache_key(
            LLMRequest(messages=[{"role": "user", "content": query}]),
            prompt_version="sem",
        ).value
        key = _store_key("semantic", version, identity)
        await self._store.put(key, json.dumps(entry, ensure_ascii=False), ttl=self._ttl)

    async def invalidate(self, request: LLMRequest, *, version: str = "v1") -> None:
        """删除相似度 ≥ threshold 的条目（语义失效）。"""
        query = _query_text(request)
        q_vec = await self._vector_of(query)
        prefix = _store_key("semantic", version, "")
        for key, raw in await self._store.scan(prefix):
            entry = _parse_entry(raw)
            if entry is None:
                continue
            if q_vec is not None and entry["vec"] is not None:
                score = _cosine(q_vec, tuple(entry["vec"]))
            else:
                score = SequenceMatcher(None, query, entry["text"]).ratio()
            if score >= self._threshold:
                await self._store.delete(key)
