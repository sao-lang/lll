"""R4-T4 成本归因测试：per-run/user/model 正确累计 + 预算硬强制（红线：路由/重试不得绕过 Budgets）。"""
from __future__ import annotations

from hooks import LLMResponse, Usage

from harness.model import CostAttributor, CostGuard, CostRecord
from tests.r4.conftest import pctx


def _record(run: str, model: str, cost: float) -> CostRecord:
    return CostRecord(
        run_id=run, agent_id="a1", user_id="u1", model=model,
        usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=cost),
    )


class TestCostAttributor:
    def test_attribution(self) -> None:
        a = CostAttributor()
        a.record(_record("r1", "m1", 0.5))
        a.record(_record("r1", "m2", 0.3))
        assert a.run_cost("r1") == 0.8
        assert a.model_cost("m1") == 0.5

    def test_snapshot(self) -> None:
        a = CostAttributor()
        a.record(_record("r1", "m1", 0.5))
        snap = a.snapshot()
        assert snap["total"] == 0.5 and snap["records"] == 1


class TestCostGuard:
    """红线：成本超预算时拒绝路由/调用，不得绕过 Budgets。"""

    def test_within_budget_allows(self) -> None:
        g = CostGuard(max_cost_usd=1.0)
        assert g.allow(0.5) is True
        g.charge(0.5)
        assert g.allow(0.5) is True

    def test_over_budget_denies(self) -> None:
        g = CostGuard(max_cost_usd=1.0)
        g.charge(0.9)
        assert g.allow(0.2) is False  # 0.9 + 0.2 > 1.0

    def test_remaining(self) -> None:
        g = CostGuard(max_cost_usd=1.0)
        g.charge(0.3)
        assert g.remaining() == 0.7
