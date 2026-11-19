"""R6 后处理段：过滤 + 预算拼接 + 引用溯源 + 注入检测接入。

PostProcessor 把召回命中整理为可注入 LLM 的 RAGResult：先过滤（含注入
fail-closed），再按 token 预算贪心装入，最后生成引用并做乱序修复。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.skills import InjectionDetector, InjectionVerdict, KeywordInjectionDetector

from harness.rag.store import matches_filters
from harness.rag.types import Hit, Query, RAGResult, TokenBudget

__all__ = ["PostProcessor", "DefaultPostProcessor", "EMPTY_CONTEXT"]

# 空结果固定提示文本（具名常量，禁魔法字符串）。
EMPTY_CONTEXT: str = "未检索到相关文档，请依据常识谨慎作答或拒绝回答。"

# 引用标注模板（具名常量）。
_CITATION_FMT: str = "[{cid}]"


@runtime_checkable
class PostProcessor(Protocol):
    """后处理协议（R6 五段之一）。"""

    name: str

    async def filter(
        self,
        hits: list[Hit],
        detector: InjectionDetector | None = None,
        filters: dict[str, object] | None = None,
    ) -> list[Hit]:
        """过滤命中（权限/时效/注入）。"""
        ...

    async def assemble(
        self,
        hits: list[Hit],
        budget: TokenBudget,
        *,
        detector: InjectionDetector | None = None,
        filters: dict[str, object] | None = None,
    ) -> RAGResult:
        """按预算拼接为 RAGResult。"""
        ...


class DefaultPostProcessor:
    """默认后处理：注入 fail-closed + 贪心预算拼接 + 引用溯源 + 乱序修复。

    乱序修复（anti lost-in-the-middle）：相关度最高的片段放两端，次高的居中，
    降低长上下文中部被忽略的风险。
    """

    name: str = "default_post"

    def __init__(self, detector: InjectionDetector | None = None) -> None:
        """构造后处理器。

        Args:
            detector: 注入检测器；None 时用 R5 默认 KeywordInjectionDetector
                （fail-closed，不存在"缺席放行"）。
        """
        self._detector: InjectionDetector = (
            detector if detector is not None else KeywordInjectionDetector()
        )

    async def filter(
        self,
        hits: list[Hit],
        detector: InjectionDetector | None = None,
        filters: dict[str, object] | None = None,
    ) -> list[Hit]:
        """兜底过滤：元数据/权限/时效 + 注入检测，两者任一不过即剔除。

        Args:
            hits: 待过滤命中。
            detector: 覆盖本次检测的注入检测器；None 用 post 自带检测器。
            filters: 元数据过滤条件（权限/时效标签等）；None 不过滤。

        Note:
            这是融合后的**统一兜底**过滤，不依赖各召回通道各自正确（B2）。
        """
        used = detector if detector is not None else self._detector
        kept: list[Hit] = []
        for hit in hits:
            if not matches_filters(hit.chunk, filters):
                continue
            verdict = used.detect(hit.chunk.text)
            if verdict is InjectionVerdict.INJECTED:
                continue
            kept.append(hit)
        return kept

    async def assemble(
        self,
        hits: list[Hit],
        budget: TokenBudget,
        *,
        detector: InjectionDetector | None = None,
        filters: dict[str, object] | None = None,
    ) -> RAGResult:
        """过滤后按预算贪心装入，生成带引用的上下文。

        Args:
            hits: 召回命中（按相关度排序）。
            budget: token 预算；limit=None 不限，0 无预算（空结果）。
            detector: 覆盖本次注入检测器（B3：让 pipeline.detector 生效）。
            filters: 元数据过滤条件，兜底施加（B2）。

        Returns:
            RAGResult；无有效命中或预算为 0 时 empty=True。
        """
        limit = budget.limit
        if limit == 0:
            return RAGResult(empty=True, context=EMPTY_CONTEXT, hits=[], citations=[])

        safe = await self.filter(hits, detector=detector, filters=filters)
        if not safe:
            return RAGResult(empty=True, context=EMPTY_CONTEXT, hits=[], citations=[])

        ordered = self._reorder_middle(safe)
        kept: list[Hit] = []
        parts: list[str] = []
        used_tokens = 0
        for hit in ordered:
            need = hit.chunk.tokens
            if limit is not None and used_tokens + need > limit:
                continue
            cite = _CITATION_FMT.format(cid=hit.chunk.chunk_id)
            parts.append(f"{hit.chunk.text} {cite}")
            kept.append(hit)
            used_tokens += need

        if not kept:
            return RAGResult(empty=True, context=EMPTY_CONTEXT, hits=[], citations=[])

        citations = [h.chunk.chunk_id for h in kept]
        return RAGResult(
            context="\n".join(parts),
            citations=citations,
            hits=kept,
            empty=False,
            tokens_used=used_tokens,
        )

    @staticmethod
    def _reorder_middle(hits: list[Hit]) -> list[Hit]:
        """把高相关片段交错放两端，低相关居中（抗 lost-in-the-middle）。"""
        if len(hits) <= 2:
            return list(hits)
        left: list[Hit] = []
        right: list[Hit] = []
        for i, hit in enumerate(hits):
            if i % 2 == 0:
                left.append(hit)
            else:
                right.append(hit)
        return left + list(reversed(right))
