"""R3 BudgetGuard 测试：软预算阈值 rewrite 收尾提示（设计 §4 声明，v2.1 落地）。"""
from __future__ import annotations

from hooks import Decision, DecisionAction, HookCtx, HookMode
from runtime import Budgets

from harness.resilience import BudgetGuard
from tests.r3.conftest import make_ctx


def _ctx_with_budget(used: int, maximum: int) -> HookCtx:
    ctx = make_ctx()
    ctx.budget_left = Budgets(max_turns=maximum, used_turns=used)
    return ctx


class TestBudgetGuard:
    def test_below_threshold_continues(self) -> None:
        guard = BudgetGuard(soft=0.8)
        d = guard.check(_ctx_with_budget(used=2, maximum=10))  # 20% < 80%
        assert d.action is DecisionAction.CONTINUE

    def test_at_soft_threshold_rewrites(self) -> None:
        guard = BudgetGuard(soft=0.8)
        d = guard.check(_ctx_with_budget(used=8, maximum=10))  # 80% ≥ 80%
        assert d.action is DecisionAction.REWRITE
        assert d.context_inject and "预算" in d.context_inject

    def test_no_budget_projection_continues(self) -> None:
        """budget_left 缺省 None（未注入预算投影）→ 放行，不误触发。"""
        guard = BudgetGuard()
        d = guard.check(make_ctx())
        assert d.action is DecisionAction.CONTINUE

    def test_cost_dimension_considered(self) -> None:
        """成本维度达软阈值也触发（多维预算任一达线即收尾）。"""
        ctx = make_ctx()
        ctx.budget_left = Budgets(max_cost_usd=10.0, used_cost_usd=9.0)  # 90%
        d = BudgetGuard(soft=0.8).check(ctx)
        assert d.action is DecisionAction.REWRITE

    def test_hook_contract(self) -> None:
        """符合 Hook 契约：name/events/mode 齐备，TRANSFORM（rewrite 生效需 transform mode）。"""
        guard = BudgetGuard()
        assert guard.name and "before_model" in guard.events
        assert guard.mode is HookMode.TRANSFORM

    def test_is_hook(self) -> None:
        from hooks import Hook
        assert isinstance(BudgetGuard(), Hook)
