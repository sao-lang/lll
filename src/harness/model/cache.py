"""R4 缓存：异步协议 + 可插拔存储工具 + 可插拔 embedding。

三层可插拔（每层都有协议 + 默认实现，符合可插拔四要素）：
1. CachePort：缓存策略协议（异步 get/put/invalidate，收 LLMRequest 原文）
   - ExactCache：精确匹配默认
   - SimilarityCache：语义相似度默认
2. CacheStore：存储工具协议（get/put/delete/scan，存 JSON 字符串）
   - InMemoryCacheStore：进程内默认
   - 用户可实现 RedisCacheStore / DBCacheStore（异步客户端直接可用）
3. CacheEmbedder：embedding 模型协议（embed）
   - 默认 HashCacheEmbedder：字符 bigram 哈希向量（零依赖、确定性），SimilarityCache 缺省走向量余弦
   - 显式 embedder=None 时降级 difflib 文本相似度（零依赖退路）
   - 用户可注入 OpenAI/本地 embedding 实现，走向量余弦相似度

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
    "DEFAULT_CACHE_EMBED_DIM",
    "ExactCache",
    "HashCacheEmbedder",
    "InMemoryCacheStore",
    "MAX_EMBED_CHARS",
    "SimilarityCache",
]

# 默认 embedding 维度：定长向量才能被 _cosine 跨条目比较（具名常量，禁魔法值）。
# 512 维而非 64 维：64 桶在长文本上哈希碰撞导致向量收敛为常量、不相关文本误命中
# （独立 review Blocker-1 实测），512 维配合二值化与窗口截断后误命中归零。
DEFAULT_CACHE_EMBED_DIM: int = 512
# 输入窗口上限：超长文本取头尾各一半，从源头限制 bigram 数，防相似度随长度收敛。
MAX_EMBED_CHARS: int = 512


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

    async def get(self, request: LLMRequest, *, version: str = "current") -> LLMResponse | None:
        """命中返回响应，未命中 None。"""
        ...

    async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "current") -> None:
        """写入响应。"""
        ...

    async def invalidate(self, request: LLMRequest, *, version: str = "current") -> None:
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


# bigram 边界哨兵：让首/尾字符也参与成组，保留位置信息（具名常量）。
_BIGRAM_START = "^"
_BIGRAM_END = "$"


class HashCacheEmbedder:
    """默认 embedding：字符 bigram 哈希到定长向量，零依赖、确定性、中文友好。

    不依赖分词库（中文无空格也能成 bigram）、不调用模型/网络/文件；用确定性的
    多项式字符哈希（**不用**进程随机化的内置 hash()），保证跨进程同文本同向量。
    向量 L2 归一化后可直接交给 _cosine 比较。

    抗收敛设计（独立 review Blocker-1）：桶二值化（出现置 1.0，不累加频次）+
    512 维 + 512 字符头尾窗口。若用计数累加，长文本各桶计数趋同为 n/64，
    向量收敛为常量导致不相关文本余弦趋近 1.0；二值化+窗口实测长文本误命中归零。
    """

    name: str = "hash_cache_embedder"

    def __init__(self, dim: int = DEFAULT_CACHE_EMBED_DIM) -> None:
        """构造默认 embedder。

        Args:
            dim: 向量维度，必须为正整数。

        Raises:
            ValueError: dim <= 0。
        """
        if dim <= 0:
            raise ValueError("dim must be a positive integer")
        self.dim = dim

    @staticmethod
    def _bigrams(text: str) -> list[str]:
        """把文本切成带边界的字符 bigram；空文本回退为单个哨兵组。

        边界哨兵让单字符文本也有向量，首字符（^x）与尾字符（x$）各成一组。
        """
        padded = _BIGRAM_START + text.lower() + _BIGRAM_END
        return [padded[i : i + 2] for i in range(len(padded) - 1)]

    def _hash_bucket(self, gram: str) -> int:
        """确定性字符哈希 → [0, dim) 桶号（多项式 31 进制，非进程随机 hash）。"""
        code = 0
        for char in gram:
            code = (code * 31 + ord(char)) % self.dim
        return code

    async def embed(self, text: str) -> tuple[float, ...]:
        """文本 → L2 归一化的定长向量（二值化 + 头尾窗口截断）。

        Args:
            text: 待向量化文本；空文本返回零向量（_cosine 保守判 0.0）。

        Returns:
            长度 self.dim 的浮点元组，各分量为 0 或 1/sqrt(命中桶数)。
        """
        # 空文本显式返回零向量：_bigrams("") 会产出 "^$" 单位向量，
        # 两个空 query 余弦=1.0 会误命中，故在源头短路（review Should-2）。
        if not text:
            return (0.0,) * self.dim
        # 超长取头尾各半：保留首尾位置信息，同时把 bigram 数限制在窗口内，
        # 防计数收敛导致不相关长文本高相似度（review Blocker-1）。
        source = text
        if len(source) > MAX_EMBED_CHARS:
            half = MAX_EMBED_CHARS // 2
            source = source[:half] + source[-half:]
        buckets: list[float] = [0.0] * self.dim
        for gram in self._bigrams(source):
            # 二值化：桶出现过置 1.0，不按频次累加——避免长文本各桶计数趋同。
            buckets[self._hash_bucket(gram)] = 1.0
        norm = math.sqrt(sum(v * v for v in buckets))
        # 理论上非空文本至少有 1 个 bigram，norm>0；保留零向量防御避免除零。
        if norm == 0.0:
            return tuple(buckets)
        return tuple(v / norm for v in buckets)


class _Unset:
    """哨兵类型：区分"embedder 未传"（用默认）与"显式 None"（降级 difflib）。"""

    def __repr__(self) -> str:
        """返回稳定标识，便于诊断。"""
        return "<UNSET>"


_UNSET = _Unset()


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

    async def get(self, request: LLMRequest, *, version: str = "current") -> LLMResponse | None:
        """命中 +1 返回响应；未命中 +1 返回 None。"""
        key = _store_key("exact", version, self._identity(request, version))
        raw = await self._store.get(key)
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return LLMResponse.model_validate_json(raw)

    async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "current") -> None:
        """写入精确键 → 响应 JSON。"""
        key = _store_key("exact", version, self._identity(request, version))
        await self._store.put(key, resp.model_dump_json(), ttl=self._ttl)

    async def invalidate(self, request: LLMRequest, *, version: str = "current") -> None:
        """删除精确键。"""
        key = _store_key("exact", version, self._identity(request, version))
        await self._store.delete(key)


# --------------------------------------------------------------------------- #
# 默认实现 2：语义相似度缓存
# --------------------------------------------------------------------------- #
class SimilarityCache:
    """语义相似度缓存：可注入 embedder（向量余弦）与 store，缺省用默认向量 embedder。

    缺省用 HashCacheEmbedder（字符 bigram 哈希，零依赖确定性）走向量余弦；
    显式传 embedder=None 时降级 difflib 文本相似度（零依赖退路）。
    两种模式都把候选存进 CacheStore（可 Redis/DB），跨实例共享。
    """

    name = "similarity_cache"

    def __init__(
        self,
        *,
        threshold: float = 0.85,
        store: CacheStore | None = None,
        embedder: CacheEmbedder | None | _Unset = _UNSET,
        ttl: int | None = None,
    ) -> None:
        """装配语义缓存。

        参数：
            threshold: 命中最低相似度（0~1），越高越保守。
            store: 存储工具（缺省内存）。
            embedder: 向量模型；未传用 HashCacheEmbedder（向量余弦），
                显式 None 降级 difflib 文本相似度，其他值按注入实现使用。
            ttl: 条目生存秒数。
        """
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self._threshold = threshold
        self._store = store if store is not None else InMemoryCacheStore()
        # 哨兵区分"未传"与"显式 None"：未传用默认哈希 embedder，显式 None 保留 difflib 降级。
        if isinstance(embedder, _Unset):
            self._embedder: CacheEmbedder | None = HashCacheEmbedder()
        else:
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

    async def get(self, request: LLMRequest, *, version: str = "current") -> LLMResponse | None:
        """按相似度找最匹配条目；≥ threshold 才命中。"""
        query = _query_text(request)
        q_vec = await self._vector_of(query)
        # 条目结构见 _SemanticCacheEntry（text/vec/resp 明确字段）。
        prefix = _store_key("semantic", version, "")
        best: LLMResponse | None = None
        best_score = 0.0
        for _key, raw in await self._store.scan(prefix):  # 遍历同前缀全部候选取最相似者；阈值裁决延到循环后，避免先到先得
            entry = _parse_entry(raw)
            if entry is None:  # 解析失败的条目跳过，不让坏数据参与匹配或污染缓存
                continue
            if q_vec is not None and entry["vec"] is not None:  # 双方都有向量才走余弦；否则降级文本相似度（零依赖开箱即用）
                score = _cosine(q_vec, tuple(entry["vec"]))
            else:
                score = SequenceMatcher(None, query, entry["text"]).ratio()
            if score > best_score:  # 只保留当前最优；遍历完整后再按 threshold 一锤定音
                best_score = score
                best = LLMResponse.model_validate_json(entry["resp"])
        if best is not None and best_score >= self._threshold:  # 阈值在循环后统一判定：命中必须是全局最优候选
            self.hits += 1
            return best
        self.misses += 1
        return None

    async def put(self, request: LLMRequest, resp: LLMResponse, *, version: str = "current") -> None:
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

    async def invalidate(self, request: LLMRequest, *, version: str = "current") -> None:
        """删除相似度 ≥ threshold 的条目（语义失效）。"""
        query = _query_text(request)
        q_vec = await self._vector_of(query)
        prefix = _store_key("semantic", version, "")
        for key, raw in await self._store.scan(prefix):  # 语义失效可能命中多条，须逐个删除，不能只删第一个
            entry = _parse_entry(raw)
            if entry is None:  # 坏条目无法算分，跳过以免影响其它候选的失效判断
                continue
            if q_vec is not None and entry["vec"] is not None:  # 与 get 同一套相似度口径，保证写入/失效判定一致
                score = _cosine(q_vec, tuple(entry["vec"]))
            else:
                score = SequenceMatcher(None, query, entry["text"]).ratio()
            if score >= self._threshold:  # 达阈值即失效：语义相同的历史条目都清掉，避免旧响应被再次命中
                await self._store.delete(key)
