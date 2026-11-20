"""R4-T4 成本归因测试：per-run/user/model 正确累计 + 预算硬强制（红线：路由/重试不得绕过 Budgets）。"""
from __future__ import annotations

from compose import LLMResponse, Usage

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

    def test_reserve_commit_adjusts_to_actual(self) -> None:
        """预留额度：调用前 reserve 占位，调用后 commit 按实际结算差额。"""
        g = CostGuard(max_cost_usd=1.0)
        r = g.reserve(0.3)
        assert r is not None
        assert g.remaining() == 0.7  # 预留立即扣减可用额度
        g.commit(r, actual=0.2)       # 实际比预估便宜，退还差额
        assert g.spent == 0.2
        assert g.remaining() == 0.8

    def test_reserve_denies_when_exhausted(self) -> None:
        """预留超预算直接拒绝（None），不留额度。"""
        g = CostGuard(max_cost_usd=0.5)
        assert g.reserve(0.3) is not None
        assert g.reserve(0.3) is None  # 0.3 + 0.3 > 0.5
        assert g.remaining() == 0.2

    def test_cancel_releases_reservation(self) -> None:
        """调用失败 cancel：释放预留，不记支出。"""
        g = CostGuard(max_cost_usd=1.0)
        r = g.reserve(0.4)
        assert r is not None
        g.cancel(r)
        assert g.remaining() == 1.0
        assert g.spent == 0.0

    def test_concurrent_reserve_never_exceeds_budget(self) -> None:
        """P2b：并发预留不超支——预算 0.5、单次预留 0.1，只允许 5 次成功。"""
        import asyncio

        g = CostGuard(max_cost_usd=0.5)

        async def try_reserve(_: int) -> bool:
            r = g.reserve(0.1)
            if r is None:
                return False
            await asyncio.sleep(0)  # 模拟调用期间的挂起
            g.commit(r, actual=0.1)
            return True

        async def run() -> list[bool]:
            return await asyncio.gather(*(try_reserve(i) for i in range(20)))

        results = asyncio.run(run())
        assert sum(results) == 5, f"预算 0.5/单次 0.1 应只放行 5 次，实际 {sum(results)}"
        assert g.spent == 0.5 and g.remaining() == 0.0
