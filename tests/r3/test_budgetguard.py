"""R3 BudgetGuard 测试：WATERFALL 回调 rewrite(value, effect) 直接返回改写文本。

领域逻辑：任一预算维度达软阈值时，在上下文文本尾部注入收尾提示；
未达阈值（含未注入预算投影）原样返回。预算投影经构造参数 budgets 注入。
"""
from __future__ import annotations

from runtime import Budgets

from harness.resilience import BudgetGuard

HINT = "Budget is running low. Wrap up and give a final answer; do not start new tasks."


class TestBudgetGuard:
    def test_below_threshold_no_rewrite(self) -> None:
        """20% < 80%：不改写，原样返回文本。"""
        guard = BudgetGuard(soft=0.8, budgets=Budgets(max_turns=10, used_turns=2))
        assert guard.refine_hint("上下文正文") == "上下文正文"

    def test_at_soft_threshold_rewrites(self) -> None:
        """80% ≥ 80%：在文本尾部追加收尾提示。"""
        guard = BudgetGuard(soft=0.8, budgets=Budgets(max_turns=10, used_turns=8))
        out = guard.refine_hint("上下文正文")
        assert out.startswith("上下文正文\n")
        assert HINT in out

    def test_no_budget_projection_no_rewrite(self) -> None:
        """budgets 未注入（None）：无信息，不误触发改写。"""
        assert BudgetGuard().refine_hint("上下文正文") == "上下文正文"

    def test_cost_dimension_considered(self) -> None:
        """成本维度达软阈值也触发（六维任一达线即收尾）。"""
        guard = BudgetGuard(
            soft=0.8, budgets=Budgets(max_cost_usd=10.0, used_cost_usd=9.0),
        )
        assert HINT in guard.refine_hint("上下文正文")

    def test_empty_value_yields_hint_only(self) -> None:
        """空文本达阈值：直接返回收尾提示，不产生多余换行。"""
        guard = BudgetGuard(soft=0.8, budgets=Budgets(max_turns=10, used_turns=8))
        assert guard.refine_hint("") == HINT
