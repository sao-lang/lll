"""R6 流水线：RAGPipeline 履约 RAGPort，编排入库与检索全链路。

双轨入口：
- ``ingest``/``retrieve`` 对齐 RAGPort（框架装配用，DTO 为 DynamicValue）；
- ``query`` 为富入口（返回结构化 RAGResult，R6 单测与 R12 精细消费用）。

检索链：改写 → 召回 → RRF（在 Retriever 内）→ 重排 → 后处理（过滤/注入/
预算拼接/引用）。关键一致性纪律：查询向量由本流水线注入的 embedder 预算
（与入库同源），detector/filters 贯穿后处理兜底，缓存键含全部影响检索的参数。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from harness.rag.embedder import Embedder
from harness.rag.post import PostProcessor
from harness.rag.rerank import Reranker
from harness.rag.retriever import Retriever
from harness.rag.rewrite import QueryRewriter
from harness.rag.splitter import Splitter
from harness.rag.store import VectorStore
from harness.rag.types import (
    Chunk,
    Document,
    Hit,
    Query,
    RAGResult,
    TokenBudget,
)
from harness.skills import InjectionDetector
from compose.tool_layer import layer_program
from compose import PortCtx
from runtime import Program

__all__ = ["PIPELINE_NAME", "CachePort", "InMemoryCache", "RAGPipeline"]

PIPELINE_NAME: str = "rag_pipeline"

# 动态值别名：RAGPort 契约要求 DynamicValue（=Any），此处集中定义一处。
type DynamicValue = Any


@runtime_checkable
class CachePort(Protocol):
    """检索缓存协议：按字符串键存取决结果，可注入替换。

    get/set/invalidate 均为同步（内存操作，无 IO）。失败返回 None 即视为未命中。
    """

    name: str

    def get(self, key: str) -> RAGResult | None:
        """按键取缓存，未命中返回 None。"""
        ...

    def set(self, key: str, value: RAGResult) -> None:
        """写入缓存。"""
        ...

    def invalidate(self, key: str | None = None) -> None:
        """失效单个键；None 表示清空全部。

        ``invalidate(key)`` 对不存在的 key 幂等（默认实现用 pop 容错）；调用方
        RAGPipeline 仅依赖单键失效，不依赖 None 全清能力——避免共享缓存被误清空。
        """
        ...


class InMemoryCache:
    """默认精确缓存实现：进程内 dict，满足 CachePort 协议。"""

    name: str = "rag_in_memory_cache"

    def __init__(self) -> None:
        """初始化空缓存。"""
        self._data: dict[str, RAGResult] = {}

    def get(self, key: str) -> RAGResult | None:
        """按键取缓存。"""
        return self._data.get(key)

    def set(self, key: str, value: RAGResult) -> None:
        """写入缓存。"""
        self._data[key] = value

    def invalidate(self, key: str | None = None) -> None:
        """失效单个键或全部。"""
        if key is None:
            self._data.clear()
        else:
            self._data.pop(key, None)


class RAGPipeline:
    """检索增强流水线，履约 ``RAGPort``。

    属性：
        name: 装配期留名（RAGPort 契约）。
        splitter/embedder/store/retriever/post: 五段主协议实现（公开以便替换断言）。
        rewrite/rerank/detector: 三个增强注入点；cache 为 CachePort 实例或 None。
        _cache_keys: 本实例写入缓存的 key 账本；ingest 后按 key 逐个失效（S4）。
    """

    name: str = PIPELINE_NAME

    def __init__(
        self,
        splitter: Splitter,
        embedder: Embedder,
        store: VectorStore,
        retriever: Retriever,
        post: PostProcessor,
        *,
        rewrite: QueryRewriter | None = None,
        rerank: Reranker | None = None,
        cache: CachePort | bool | None = None,
        detector: InjectionDetector | None = None,
    ) -> None:
        """构造流水线，注入点中五段必填、四个增强可选。

        Args:
            splitter: 切分段。
            embedder: 向量化段（入库与查询共用，保证同一语义空间）。
            store: 存储段。
            retriever: 召回段。
            post: 后处理段。
            rewrite: 查询改写器；None 表示跳过改写。
            rerank: 重排器；None 表示跳过重排。
            cache: CachePort 实例（启用并委托）/ True（内置 InMemoryCache）/ None 或 False（关闭）。
            detector: 注入检测器；None 时由 post 自带检测器兜底。
        """
        self.splitter = splitter
        self.embedder = embedder
        self.store = store
        self.retriever = retriever
        self.post = post
        self.rewrite = rewrite
        self.rerank = rerank
        self.detector = detector
        # cache 统一归一为 CachePort 实例或 None（S5：注入对象真正生效）。
        if cache is True:
            self._cache: CachePort | None = InMemoryCache()
        elif cache is False or cache is None:
            self._cache = None
        else:
            self._cache = cache
        # S4：记录本实例写入缓存的 key，ingest 后按 key 精准失效而非全表清空，
        # 共享 CachePort 时不会误删其他实例的条目（FR-1）。
        self._cache_keys: set[str] = set()

    async def ingest(self, documents: list[DynamicValue], ctx: PortCtx) -> int:
        """RAGPort 入口：切分→向量化→入库，返回成功入库块数。

        Args:
            documents: Document 实例（DynamicValue 边界，运行期按鸭子协议读取）。
            ctx: Port 上下文（签名履约 RAGPort，当前不消费）。

        Returns:
            成功入库的块数；入库成功后按 key 失效本实例写入的缓存（S4：新文档对已缓存 query 可见）。
        """
        del ctx
        chunks: list[Chunk] = []
        for document in documents:
            doc = (
                document
                if isinstance(document, Document)
                else Document.model_validate(document)
            )
            chunks.extend(self.splitter.split(doc))
        if not chunks:
            return 0
        vectors = await self.embedder.embed([c.text for c in chunks])
        for chunk, vec in zip(chunks, vectors):
            chunk.vector = vec
        await self.store.upsert(chunks)
        await self.invalidate_cache()
        return len(chunks)

    async def retrieve(self, query: str, ctx: PortCtx) -> list[DynamicValue]:
        """RAGPort 入口：str → Query（预算取 ctx）→ query() → 引用投影。

        Args:
            query: 查询文本。
            ctx: Port 上下文，budget_left 派生 TokenBudget。

        Returns:
            命中列表，每项含 ``citation``/``doc_id``/``text``/``score``/``meta``。
        """
        budget = self._budget_from_ctx(ctx)
        q = Query(text=query, token_budget=budget)
        result = await self.query(q)
        return [
            {
                "citation": h.chunk.chunk_id,
                "doc_id": h.chunk.doc_id,
                "text": h.chunk.text,
                "score": h.score,
                "meta": dict(h.chunk.meta),
            }
            for h in result.hits
        ]

    async def query(self, q: Query) -> RAGResult:
        """富入口：完整检索链，返回结构化 RAGResult。

        流程：改写 → （用注入 embedder 预算查询向量）→ 召回 → 重排 →
        后处理（filters 兜底 + 注入 fail-closed + 预算拼接 + 引用）。
        跨改写 query 的命中按 chunk_id 去重（S7）。缓存命中直接返回。
        """
        cache_key = self._cache_key(q)
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        queries = [q]
        if self.rewrite is not None:
            rewritten = await self.rewrite.rewrite(q)
            if rewritten:
                queries = list(rewritten)

        # B1：查询向量用本流水线注入的 embedder 计算，与入库同一空间。
        vecs = await self.embedder.embed([sub.text for sub in queries])
        hits: list[Hit] = []
        for sub, vec in zip(queries, vecs):
            hits.extend(await self.retriever.retrieve(sub, vec))
        hits = self._dedup(hits)
        if self.rerank is not None:
            hits = self.rerank.rerank(hits, q)

        result = await self.post.assemble(
            hits,
            TokenBudget(limit=q.token_budget),
            detector=self.detector,
            filters=q.filters,
        )
        if self._cache is not None:
            self._cache.set(cache_key, result)
            # S4：登记本实例写入的 key，供 ingest 后逐 key 失效（FR-2）。
            self._cache_keys.add(cache_key)
        return result

    def build(self) -> Program:
        """ToolLayer 外壳（规格 P7）：产出"用 RAG"的 Program 工厂。

        九注入点内部不改（query 链照旧）；外壳只给 R1 领域无知的 Program，
        供"挑选 + A 类组合 + R1 只认 Program"试点验证（tests/r12/test_r6_pilot.py）。
        """
        return layer_program("rag", payload={"layer": "rag"})()

    def with_(self, **replacements: object) -> RAGPipeline:
        """不可变替换：返回注入点被替换的新实例，原实例不变（S1）。

        全部 8 个注入点的当前值都被复制；未知替换点抛 ValueError。
        """
        allowed = {
            "splitter", "embedder", "store", "retriever", "post",
            "rewrite", "rerank", "cache", "detector",
        }
        unknown = set(replacements) - allowed
        if unknown:
            raise ValueError(f"Unknown replacement points: {sorted(unknown)}")
        # cache 归一：保留原 CachePort 实例（或 None）；账本归属见下方 FR-5。
        current: dict[str, object] = {
            "splitter": self.splitter,
            "embedder": self.embedder,
            "store": self.store,
            "retriever": self.retriever,
            "post": self.post,
            "rewrite": self.rewrite,
            "rerank": self.rerank,
            "cache": self._cache,
            "detector": self.detector,
        }
        current.update(replacements)
        new = type(self)(**current)  # type: ignore[arg-type]
        # S4 账本归属（FR-5）：cache 实例未变（含未传）→ 共享同一账本，否则
        # 新实例 ingest 失效不到旧实例写入的 key，破 S4；cache 被换 → 新实例空账本。
        if new._cache is self._cache:
            new._cache_keys = self._cache_keys
        return new

    async def invalidate_cache(self) -> None:
        """按 key 失效本实例写入的检索缓存（不无参全表清空）。

        FR-3：对账本中每个 key 调 ``CachePort.invalidate(key)``，随后清空账本；
        默认内存实现与自定义共享缓存走同一路径，避免误删他人的缓存条目。
        """
        if self._cache is None or not self._cache_keys:
            return
        # 先固定本次失效集合：CachePort 契约为同步（§CachePort）、本方法内无 await，
        # asyncio 单线程下循环期间不会被任务切换插入登记，snapshot == 当前账本。
        # 用 difference_update 而非 clear：仅移除已失效的 key，若未来换成可挂起/跨线程
        # 实现，循环期间新登记的 key 会保留到下次 ingest，不会"有缓存无账本"泄漏。
        snapshot = tuple(self._cache_keys)
        for key in snapshot:
            self._cache.invalidate(key)
        self._cache_keys.difference_update(snapshot)

    @staticmethod
    def _cache_key(q: Query) -> str:
        """缓存键纳入全部影响检索的参数（S4）：文本/top_k/预算/用途/过滤条件。"""
        filters_repr = repr(sorted((q.filters or {}).items()))
        return f"{q.text}|{q.top_k}|{q.token_budget}|{q.purpose.value}|{filters_repr}"

    @staticmethod
    def _dedup(hits: list[Hit]) -> list[Hit]:
        """按 chunk_id 去重保留最高分（S7：多 query 改写不产生重复引用）。"""
        best: dict[str, Hit] = {}
        for hit in hits:
            cid = hit.chunk.chunk_id
            if cid not in best or hit.score > best[cid].score:
                best[cid] = hit
        return sorted(best.values(), key=lambda h: h.score, reverse=True)

    @staticmethod
    def _budget_from_ctx(ctx: PortCtx) -> int | None:
        """从 PortCtx.budget_left 派生预算（S3）。

        - None → 不限制（返回 None）。
        - int → 余量（>=0）；0 表示耗尽，返回 0（后处理转空结果，绝不当不限）。
        - R1 Budgets 投影（含 max_tokens/used_tokens）→ 计算余量。
        - 无法识别 → None（不限制，保守放行由装配层保证传入正确投影）。
        """
        raw = ctx.budget_left
        if raw is None:
            return None
        if isinstance(raw, int):
            return max(0, raw)
        max_tokens = getattr(raw, "max_tokens", None)
        used_tokens = getattr(raw, "used_tokens", None)
        if isinstance(max_tokens, int) and isinstance(used_tokens, int):
            return max(0, max_tokens - used_tokens)
        for attr in ("tokens", "token_budget", "total_tokens"):
            value = getattr(raw, attr, None)
            if isinstance(value, int):
                return max(0, value)
        return None
