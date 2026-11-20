"""R7 MemoryPort 默认实现：双轴分类 + 四条优化链 + 递归安全检测。

- 温度轴 tier（hot/warm/cold）× 内容轴 kind（七类）正交；各层独立 TTL。
- 写入链：规范化→递归脱敏/注入→去重→冲突消解→评分→分层落库→稳定 id upsert。
- 召回链：tenant 过滤→混合召回→rerank 加权和→MMR 去冗余→预算裁剪→注入复检。
- read/write/forget 保持 R2 MemoryPort 签名；旧调用方五字段 MemoryItem 兼容。
"""
from __future__ import annotations

from asyncio import get_running_loop, run as asyncio_run
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from math import exp, log
from time import time_ns
from typing import TypeVar

from harness.rag.embedder import Embedder
from harness.rag.store import VectorStore
from harness.rag.types import Chunk, Vector
from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector
from compose import PortCtx
from runtime import DynamicValue

from .types import (
    MMR_LAMBDA,
    NEAR_DUP_SIM_THRESHOLD,
    PROFILE_CONFIDENCE_THRESHOLD,
    RERANK_W_CONFIDENCE,
    RERANK_W_IMPORTANCE,
    RERANK_W_RECENCY,
    RERANK_W_RELEVANCE,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryTier,
    ScopeResolver,
    is_secret_key,
    make_item_id,
)

__all__ = ["MemoryStore"]

# 分层顺序：索引越小越热；越大越冷。
_TIER_ORDER: tuple[MemoryTier, ...] = (MemoryTier.HOT, MemoryTier.WARM, MemoryTier.COLD)
# 各层默认 TTL（纳秒）：hot 1 小时 / warm 30 天 / cold 365 天。
_TIER_TTL_NS: dict[MemoryTier, int] = {
    MemoryTier.HOT: 3_600_000_000_000,
    MemoryTier.WARM: 2_592_000_000_000_000,
    MemoryTier.COLD: 31_536_000_000_000_000,
}
# kind → 初始层默认落点（不破坏正交：任何 kind 都可因访问升迁/沉降）。
_KIND_DEFAULT_TIER: dict[MemoryKind, MemoryTier] = {
    MemoryKind.PROFILE: MemoryTier.WARM,
    MemoryKind.PREFERENCE: MemoryTier.WARM,
    MemoryKind.ENTITY: MemoryTier.WARM,
    MemoryKind.SEMANTIC: MemoryTier.WARM,
    MemoryKind.PROCEDURAL: MemoryTier.WARM,
    MemoryKind.EPISODIC: MemoryTier.HOT,
    MemoryKind.JOURNAL: MemoryTier.COLD,
}
_RECALL_CHUNK_PREFIX = "mem:"
# importance 命中增量上限与系数（命中后 log 增长，避免线性膨胀）。
_IMPORTANCE_BOOST = 0.02
_MAX_IMPORTANCE_GAIN = 0.2
# 时效半衰期（30 天，纳秒）。
_HALF_LIFE_NS = 2_592_000_000_000_000

# 协程返回值泛型变量（供 _run_sync 类型推断）。
_T = TypeVar("_T")


def _run_sync(coro: Coroutine[object, object, _T]) -> _T:
    """在同步上下文运行协程；已有事件循环时新建线程避免冲突。

    Args:
        coro: 待执行的协程对象。

    Returns:
        协程返回值（类型由泛型参数推断）。
    """
    try:
        get_running_loop()
    except RuntimeError:
        return asyncio_run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio_run, coro).result()


def _run_typed(coro: Coroutine[object, object, list[Vector]]) -> list[Vector]:
    """显式收窄向量化协程返回类型（Embedder.embed 契约）。"""
    return _run_sync(coro)


class MemoryStore:
    """R7 长期记忆默认实现：双轴分层 + 写入/召回优化链 + 递归安全检测。

    Attributes:
        _embedder: 语义召回向量化后端；None 时用 R6 HashEmbedder。
        _vector_store: 语义召回向量库；None 时用 R6 InMemoryVectorStore。
        _detector: 注入检测器；None 时用 R5 KeywordInjectionDetector。
        _kv: (namespace, key) → 值，键值记忆本体（read/write/forget 操作）。
        _items: item_id → MemoryItem，长期记忆项实体（召回与治理操作）。
        _tiers: item_id → 当前温度层。
        _item_times: item_id → 写入时刻（ns），供独立 TTL 判定。
        _vectors: item_id → 已 embed 的向量，供稳定增量召回。
    """

    name: str = "memory_store"

    def __init__(
        self,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        detector: InjectionDetector | None = None,
    ) -> None:
        """构造记忆存储；None 依赖用 R6/R5 默认实现降级。

        Args:
            embedder: 向量化后端；None 用 R6 HashEmbedder。
            vector_store: 向量库；None 用 R6 InMemoryVectorStore。
            detector: 注入检测器；None 用 R5 KeywordInjectionDetector。
        """
        from harness.rag.embedder import HashEmbedder
        from harness.rag.store import InMemoryVectorStore

        self._embedder = embedder if embedder is not None else HashEmbedder()
        self._vector_store = (
            vector_store if vector_store is not None else InMemoryVectorStore()
        )
        self._detector = (
            detector if detector is not None else KeywordInjectionDetector()
        )
        self._kv: dict[tuple[str, str], DynamicValue] = {}
        self._items: dict[str, MemoryItem] = {}
        self._tiers: dict[str, MemoryTier] = {}
        self._item_times: dict[str, int] = {}
        self._vectors: dict[str, tuple[float, ...]] = {}

    # ---------- R2 MemoryPort 履约（保持签名） ----------
    async def read(self, namespace: str, key: str, ctx: PortCtx) -> DynamicValue:
        """按 namespace/key 读取；不存在返回 None（键值层不做 tenant 隔离）。"""
        return self._kv.get((namespace, key))

    async def write(
        self, namespace: str, key: str, value: DynamicValue, ctx: PortCtx
    ) -> None:
        """写入键值记忆；递归 secret/注入检测命中即拒绝（fail-closed）。"""
        if is_secret_key(key) or is_secret_key(namespace):
            raise ValueError(f"secret key rejected: {key}")
        self._scan_value(value, key)
        self._kv[(namespace, key)] = value

    async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:
        """遗忘指定键值；不存在幂等。"""
        self._kv.pop((namespace, key), None)

    async def recall(self, query: MemoryQuery, ctx: PortCtx) -> list[MemoryItem]:
        """完整召回链：tenant 过滤→混合召回→rerank 加权和→MMR→注入复检。"""
        scope = ScopeResolver.resolve(ctx)
        ScopeResolver.check(query.scope, scope)
        candidates = [
            item
            for item in self._items.values()
            if item.scope is not None and item.scope.tenant == scope.tenant
        ]
        if query.tier is not None:
            candidates = [it for it in candidates if self.tier_of(it) is query.tier]
        if query.kind is not None:
            candidates = [it for it in candidates if it.kind is query.kind]
        if not candidates:
            return []
        scored = self._score_candidates(query.text, candidates)
        ranked = sorted(scored, key=lambda pair: pair[1], reverse=True)
        selected = self._mmr(ranked)
        out: list[MemoryItem] = []
        for item, _ in selected[: query.top_k]:
            if self._scan_text(item.content):
                continue  # 历史脏数据注入复检：命中即跳过（fail-closed）
            fresh = self.record_access(item)
            out.append(fresh if fresh is not None else item)
        return out

    # ---------- 分层与治理 ----------
    def write_cold(self, transcript: str, ctx: PortCtx | None = None) -> None:
        """先落盘原文全文（commit_and_compact 第一步），不经过提炼链。"""
        scope = (
            ScopeResolver.resolve(ctx)
            if ctx is not None
            else MemoryScope(tenant="default")
        )
        item = MemoryItem(
            kind=MemoryKind.EPISODIC,
            content=transcript,
            importance=0.5,
            ts=time_ns(),
            source="cold:transcript",
            item_id=make_item_id(scope.tenant, MemoryKind.EPISODIC, transcript),
            tier=MemoryTier.COLD,
            scope=scope,
        )
        self.put_item(item, MemoryTier.COLD, now=item.ts)

    def ingest(
        self,
        text: str,
        ctx: PortCtx,
        *,
        kind: MemoryKind | None = None,
        confidence: float = 1.0,
        source: str = "refined",
        now: int | None = None,
    ) -> MemoryItem | None:
        """完整写入链：判定 kind → 评分 → 去重/冲突消解 → 默认层落库。

        Args:
            text: 待提炼的正文（已由调用方确保不含 secret）。
            ctx: Port 上下文，提供 scope。
            kind: 显式内容类型；None 时按 §12.2 规则自动判定。
            confidence: 来源置信度 0~1，用于冲突裁决。
            source: 来源标识。
            now: 写入时刻；None 用当前时刻。

        Returns:
            落库的 MemoryItem；被判为近重复而合并时返回已存项。
        """
        scope = ScopeResolver.resolve(ctx)
        chosen = kind if kind is not None else self.refine_kind(text, confidence)
        normalized = " ".join(text.split())
        item_id = make_item_id(scope.tenant, chosen, normalized)
        timestamp = now if now is not None else time_ns()
        # 冲突消解：同 id 已存在时按 confidence 裁决，低置信不覆盖高置信。
        existing = self._items.get(item_id)
        if existing is not None and existing.confidence > confidence:
            return existing
        # 近重复检测：同 tenant 内高相似先合并（写入链也用 0.92 阈值）。
        dup = self._find_near_duplicate(normalized, scope.tenant)
        if dup is not None and dup.confidence >= confidence:
            return dup
        importance = self.score_importance(normalized, chosen, source)
        item = MemoryItem(
            kind=chosen,
            content=normalized,
            importance=importance,
            ts=timestamp,
            source=f"{source}:{chosen.value}",
            item_id=item_id,
            confidence=confidence,
            tier=self.default_tier_for(chosen),
            scope=scope,
        )
        self.put_item(item, item.tier, now=timestamp)
        return item

    @staticmethod
    def refine_kind(text: str, confidence: float) -> MemoryKind:
        """按 §12.2 互斥优先级判定内容类型（命中即停）。

        Args:
            text: 规范化正文。
            confidence: 来源置信度（profile 高置信推断阈值）。

        Returns:
            判定的 MemoryKind；兜底 SEMANTIC。
        """
        folded = text.casefold()
        if any(tok in folded for tok in ("要", "不要", "总是", "禁止", "prefer", "must")):
            return MemoryKind.PREFERENCE
        if confidence >= PROFILE_CONFIDENCE_THRESHOLD and any(
            tok in folded for tok in ("我是", "角色", "职业", "领域", "工程师", "身份")
        ):
            return MemoryKind.PROFILE
        if any(tok in folded for tok in ("决定了", "因为", "选择", "决策")):
            return MemoryKind.JOURNAL
        if any(tok in folded for tok in ("步骤", "流程", "先", "然后", "recipe")):
            return MemoryKind.PROCEDURAL
        if any(tok in folded for tok in ("发生", "上次", "昨天", "参加了")):
            return MemoryKind.EPISODIC
        if any(tok in folded for tok in ("项目", "工具", "实体", "实体卡")):
            return MemoryKind.ENTITY
        return MemoryKind.SEMANTIC

    @staticmethod
    def score_importance(text: str, kind: MemoryKind, source: str) -> float:
        """importance 评分：0.35·信息量 + 0.25·复用度 + 0.20·时效 + 0.20·来源权重。

        Args:
            text: 规范化正文。
            kind: 内容类型（复用度按类型给基准分）。
            source: 来源标识（显式来源给高权重）。

        Returns:
            0~1 的重要性评分。
        """
        info = min(1.0, len(set(text.split())) / 20.0)
        reuse = 1.0 if kind in (MemoryKind.PROCEDURAL, MemoryKind.PREFERENCE) else 0.6
        recency = 1.0  # 新写入项时效满分；随时间衰减由 _recency 在召回期计算
        weight = 1.0 if source.startswith("explicit") else 0.7
        return min(1.0, 0.35 * info + 0.25 * reuse + 0.20 * recency + 0.20 * weight)

    def _find_near_duplicate(self, text: str, tenant: str) -> MemoryItem | None:
        """写入链近重复检测：同 tenant 内相似度 ≥ 阈值即视为重复。

        Args:
            text: 规范化正文。
            tenant: 隔离域租户。

        Returns:
            首个高相似已存项；无则 None。
        """
        vec = self._embed_sync(text)
        for item in self._items.values():
            if item.scope.tenant != tenant:
                continue
            if self._dot(vec, self._vector_of(item)) >= NEAR_DUP_SIM_THRESHOLD:
                return item
        return None

    def put_item(
        self,
        item: MemoryItem,
        tier: MemoryTier,
        *,
        now: int | None = None,
    ) -> None:
        """登记一条记忆项；同 item_id 覆盖不追加（稳定 upsert）。"""
        item_id = item.item_id or make_item_id(item.scope.tenant, item.kind, item.content)
        ts = now if now is not None else item.ts
        self._items[item_id] = item
        self._tiers[item_id] = tier
        self._item_times[item_id] = ts

    def tier_of(self, item: MemoryItem) -> MemoryTier | None:
        """查当前层；先按稳定 id，回退对象身份/值比对（兼容旧调用方）。"""
        if item.item_id and item.item_id in self._tiers:
            return self._tiers[item.item_id]
        for item_id, stored in self._items.items():
            if stored is item or stored == item:
                return self._tiers.get(item_id)
        return None

    def tier_count(self) -> int:
        """返回已登记记忆项总数（诊断/测试用）。"""
        return len(self._items)

    def promote(self, item: MemoryItem) -> None:
        """加深归档一级：hot→warm→cold；已在 cold 不动。"""
        self._shift(item, +1)

    def demote(self, item: MemoryItem) -> None:
        """回温一级：cold→warm→hot；已在 hot 不动。"""
        self._shift(item, -1)

    def forget_expired(
        self, ttl: int | None = None, *, now: int | None = None
    ) -> list[MemoryItem]:
        """遗忘过期项；ttl 缺省按各层独立 TTL，now 缺省为当前时刻。"""
        moment = now if now is not None else time_ns()
        expired: list[MemoryItem] = []
        for item_id, ts in list(self._item_times.items()):
            effective = (
                ttl
                if ttl is not None
                else _TIER_TTL_NS.get(self._tiers[item_id], _TIER_TTL_NS[MemoryTier.COLD])
            )
            if moment - ts > effective:
                item = self._items.pop(item_id, None)
                self._tiers.pop(item_id, None)
                self._item_times.pop(item_id, None)
                self._vectors.pop(item_id, None)
                if item is not None:
                    expired.append(item)
        return expired

    def importance_of(self, item: MemoryItem) -> float:
        """读取当前 importance（命中重算后可能高于初始值）。"""
        if item.item_id and item.item_id in self._items:
            return self._items[item.item_id].importance
        return item.importance

    def record_access(self, item: MemoryItem) -> MemoryItem | None:
        """记录命中：access_count+1 且 importance 按 log 增长重算。

        Args:
            item: 被命中的记忆项。

        Returns:
            更新后的 MemoryItem（caller 应使用返回值）；未登记返回 None。
        """
        item_id = item.item_id or make_item_id(item.scope.tenant, item.kind, item.content)
        stored = self._items.get(item_id)
        if stored is None:
            return None
        count = stored.access_count + 1
        boost = min(_MAX_IMPORTANCE_GAIN, _IMPORTANCE_BOOST * log(1 + count))
        updated = MemoryItem(
            kind=stored.kind,
            content=stored.content,
            importance=min(1.0, stored.importance + boost),
            ts=stored.ts,
            source=stored.source,
            item_id=stored.item_id,
            confidence=stored.confidence,
            tier=stored.tier,
            scope=stored.scope,
            access_count=count,
        )
        self._items[item_id] = updated
        return updated

    def default_tier_for(self, kind: MemoryKind) -> MemoryTier:
        """查询内容类的初始层默认落点（维度正交，仅默认非强制）。"""
        return _KIND_DEFAULT_TIER.get(kind, MemoryTier.WARM)

    # ---------- 内部：召回链 ----------
    def _score_candidates(
        self, text: str, items: list[MemoryItem]
    ) -> list[tuple[MemoryItem, float]]:
        """混合召回打分（词面重合 + 向量相似）+ rerank 加权和。"""
        query_lower = text.casefold()
        lexical = [
            sum(1.0 for word in query_lower.split() if word in it.content.casefold())
            for it in items
        ]
        max_lex = max(lexical, default=1.0) or 1.0
        vectors = self._ensure_vectors(items)
        qvec = self._embed_sync(text)
        scored: list[tuple[MemoryItem, float]] = []
        for idx, item in enumerate(items):
            sim = self._dot(qvec, vectors[idx])
            relevance = 0.6 * sim + 0.4 * (lexical[idx] / max_lex)
            score = (
                RERANK_W_RELEVANCE * relevance
                + RERANK_W_IMPORTANCE * item.importance
                + RERANK_W_RECENCY * self._recency(item.ts)
                + RERANK_W_CONFIDENCE * item.confidence
            )
            scored.append((item, score))
        return scored

    def _mmr(
        self, ranked: list[tuple[MemoryItem, float]]
    ) -> list[tuple[MemoryItem, float]]:
        """MMR 贪心重排：每轮选 λ·相关 - (1-λ)·最大相似度 最优者。

        Args:
            ranked: rerank 后的 (item, score) 候选，按 score 降序。

        Returns:
            去冗余后的 (item, mmr_score) 序列（真贪心顺序，非输入顺序）。
        """
        remaining = list(ranked)
        selected: list[tuple[MemoryItem, float]] = []
        while remaining:
            best_idx = 0
            best_score = float("-inf")
            for idx, (item, score) in enumerate(remaining):
                max_sim = (
                    max(
                        self._dot(self._vector_of(item), self._vector_of(other))
                        for other, _ in selected
                    )
                    if selected
                    else 0.0
                )
                mmr_score = MMR_LAMBDA * score - (1 - MMR_LAMBDA) * max_sim
                if mmr_score > best_score:
                    best_score, best_idx = mmr_score, idx
            chosen = remaining.pop(best_idx)
            selected.append((chosen[0], best_score))
        return selected

    def _ensure_vectors(self, items: list[MemoryItem]) -> list[tuple[float, ...]]:
        """按稳定 item_id 惰性 embed 并缓存（禁止全量重嵌/临时索引）。"""
        self._embed_sync_batch([it for it in items if it.item_id not in self._vectors])
        return [self._vector_of(it) for it in items]

    def _embed_sync_batch(self, items: list[MemoryItem]) -> None:
        """把未缓存项 embed 后写入向量库（chunk_id 为稳定 item_id）。"""
        if not items:
            return
        vectors = _run_typed(self._embedder.embed([it.content for it in items]))
        for item, vec in zip(items, vectors):
            if item.item_id:
                self._vectors[item.item_id] = tuple(vec)
                chunk = Chunk(
                    chunk_id=f"{_RECALL_CHUNK_PREFIX}{item.item_id}",
                    doc_id="memory",
                    text=item.content,
                    vector=tuple(vec),
                )
                _run_sync(self._vector_store.upsert([chunk]))

    def _embed_sync(self, text: str) -> tuple[float, ...]:
        """查询文本 embed（同步包装）；失败返回空向量走词面兜底。"""
        try:
            vectors = _run_typed(self._embedder.embed([text]))
            return tuple(vectors[0]) if vectors else ()
        except (IndexError, ValueError, TypeError):
            return ()

    def _vector_of(self, item: MemoryItem) -> tuple[float, ...]:
        """取项向量；未缓存则即时 embed。"""
        if item.item_id and item.item_id in self._vectors:
            return self._vectors[item.item_id]
        vec = self._embed_sync(item.content)
        if item.item_id:
            self._vectors[item.item_id] = vec
        return vec

    @staticmethod
    def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        """点积相似度；维度不等返回 0（安全降级）。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))

    @staticmethod
    def _recency(ts: int) -> float:
        """时效分数：30 天半衰期归一到 0~1。"""
        age_ns = max(0, time_ns() - ts)
        return exp(-age_ns / _HALF_LIFE_NS)

    # ---------- 内部：治理与安全 ----------
    def _shift(self, item: MemoryItem, delta: int) -> None:
        """按 delta 在温度层序上移动一级，不改内容轴 kind。"""
        item_id = item.item_id or make_item_id("default", item.kind, item.content)
        current = self._tiers.get(item_id)
        if current is None:
            for mid, stored in self._items.items():
                if stored is item or stored == item:
                    item_id, current = mid, self._tiers.get(mid)
                    break
        if current is None:
            return
        idx = _TIER_ORDER.index(current)
        self._tiers[item_id] = _TIER_ORDER[
            min(max(idx + delta, 0), len(_TIER_ORDER) - 1)
        ]

    def _scan_value(self, value: DynamicValue, label: str) -> None:
        """递归扫描任意值（含 dict/list/tuple 嵌套字符串）的 secret/注入串。"""
        if isinstance(value, str):
            if is_secret_key(label) or self._scan_text(value):
                raise ValueError(f"secret/injection rejected in value: {label}")
        elif isinstance(value, dict):
            for key, sub in value.items():
                self._scan_value(sub, str(key))
        elif isinstance(value, (list, tuple, set, frozenset)):
            for sub in value:
                self._scan_value(sub, label)
        elif hasattr(value, "model_dump"):
            # pydantic 模型：展开为 dict 后递归，防止嵌套 secret 绕过。
            self._scan_value(value.model_dump(), label)
        elif isinstance(value, bytes):
            self._scan_value(value.decode("utf-8", errors="replace"), label)

    def _scan_text(self, text: str) -> bool:
        """复用 R5 检测器判定注入串。"""
        return self._detector.detect(text) is InjectionVerdict.INJECTED
