"""R7 代码阅读指南 §7 陷阱修复回归（逐条勾销）。

覆盖源码侧 4 个真实缺口：
- 陷阱2：PREFERENCE_REPEAT_THRESHOLD 未落地（同类约束≥3次判定）。
- 陷阱3：七类互斥优先级与详设 §12.2 一致（entity 在 procedural/journal 前）。
- 陷阱8：promote=回温、demote=沉降（对齐详设语义与词面直觉）。
- 陷阱9：MemoryQuery.budget 贪心裁剪（非纯 top_k 截断）。
- 陷阱11：SUMMARY_ROLE 单一定义源。
"""
from __future__ import annotations

from harness.memory import MemoryItem, MemoryKind, MemoryStore, MemoryTier
from harness.memory.types import MemoryQuery, PREFERENCE_REPEAT_THRESHOLD
from harness.rag.types import TokenBudget

from tests.r7.conftest import pctx  # type: ignore[import-not-found]


# ---------- 陷阱2：偏好重复阈值落地 ----------
def test_preference_repeat_threshold_is_applied() -> None:
    """同类约束出现≥阈值次，即使无显式偏好词也判 PREFERENCE。"""
    # refine_kind 新增可选 preference_hits 参数；累计命中达阈值即判偏好
    assert MemoryStore.refine_kind(
        "总是早睡", 0.5, preference_hits=PREFERENCE_REPEAT_THRESHOLD
    ) is MemoryKind.PREFERENCE
    # 未达阈值：显式偏好词缺席时仍是 SEMANTIC（普通陈述）
    assert MemoryStore.refine_kind(
        "早睡有益", 0.5, preference_hits=PREFERENCE_REPEAT_THRESHOLD - 1
    ) is not MemoryKind.PREFERENCE
    # 显式偏好词始终优先（即使 hits=0）
    assert MemoryStore.refine_kind("禁止迟到", 0.5, preference_hits=0) is MemoryKind.PREFERENCE


# ---------- 陷阱3：七类优先级对齐详设 §12.2 ----------
def test_kind_priority_entity_before_journal_and_procedural() -> None:
    """详设 §12.2 权威序：preference→profile→entity→procedural→journal→episodic→semantic。

    含具名实体 + 属性 + 步骤词的文本应判 ENTITY（entity 优先于 procedural/journal）。
    """
    kind = MemoryStore.refine_kind("项目 Alpha 的步骤：先分析然后实现", 0.5)
    assert kind is MemoryKind.ENTITY


# ---------- 陷阱8：promote/demote 语义纠正 ----------
def test_promote_warms_and_demote_deepens() -> None:
    """promote=回温（cold→warm→hot），demote=沉降（hot→warm→cold）。"""
    ms = MemoryStore()
    item = MemoryItem(MemoryKind.SEMANTIC, "事实", 0.9, 1, "test")
    ms.put_item(item, MemoryTier.COLD)
    ms.promote(item)  # cold → warm（回温）
    assert ms.tier_of(item) is MemoryTier.WARM
    ms.promote(item)  # warm → hot
    assert ms.tier_of(item) is MemoryTier.HOT
    ms.demote(item)  # hot → warm（沉降）
    assert ms.tier_of(item) is MemoryTier.WARM


# ---------- 陷阱9：预算裁剪（贪心装入）----------
def test_recall_budget_caps_results_not_only_top_k() -> None:
    """MemoryQuery.budget 存在时按预算裁剪，结果条数 ≤ 预算允许；无 budget 仍按 top_k。"""
    import asyncio

    async def run() -> None:
        ms = MemoryStore()
        ctx = pctx("tb")
        for i in range(10):
            ms.ingest(f"独立事实 {i}", ctx, kind=MemoryKind.SEMANTIC)
        # budget 给很小 → 裁剪条数应少于 top_k=10
        q = MemoryQuery(text="事实", top_k=10, budget=TokenBudget(limit=20))
        out = await ms.recall(q, ctx)
        assert 0 < len(out) < 10, f"budget 应限制条数，实际 {len(out)}"
        # 无 budget → top_k 生效
        assert len(await ms.recall(MemoryQuery(text="事实", top_k=3), ctx)) <= 3

    asyncio.run(run())


# ---------- 陷阱11：SUMMARY_ROLE 单一定义源 ----------
def test_summary_role_single_source() -> None:
    """SUMMARY_ROLE 只有一处定义，reinjector 从 compaction 导入。"""
    from harness.context.compaction import SUMMARY_ROLE as FROM_COMPACTION
    from harness.context.reinjector import SUMMARY_ROLE as FROM_REINJECTOR

    assert FROM_COMPACTION is FROM_REINJECTOR  # 同一对象（单一源）
