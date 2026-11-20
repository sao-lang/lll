"""R4-T3 模型路由测试：按任务/难度/成本选模型 + 决策进 trace + 未注册目标拒绝。"""
from __future__ import annotations

from runtime import Effect, Invoke

from harness.model import ModelRouter, RuleRouter, RouteDecision
from tests.r4.conftest import pctx, req


class TestRuleRouter:
    def test_route_by_task_hint(self) -> None:
        """按 task_hint 选模型（如 code→强模型）。"""
        router = RuleRouter(
            routes={"code": "gpt-4", "chat": "gpt-3.5"},
            registered={"gpt-4", "gpt-3.5"},
        )
        effect = Invoke(port="llm", payload={'messages': [], 'task_hint': 'code'})
        d = router.route(effect, pctx())
        assert d.model == "gpt-4"

    def test_fallback_to_default(self) -> None:
        """未知 task_hint → 默认模型。"""
        router = RuleRouter(
            routes={"code": "gpt-4"}, default="gpt-3.5",
            registered={"gpt-4", "gpt-3.5"},
        )
        effect = Invoke(port="llm", payload={'messages': [], 'task_hint': 'unknown'})
        assert router.route(effect, pctx()).model == "gpt-3.5"

    def test_cost_tier_selection(self) -> None:
        """按成本预算选便宜模型（budget_hint 低→便宜）。"""
        router = RuleRouter(
            routes={"code": "gpt-4"},
            cost_tiers={"gpt-4": 0.03, "gpt-3.5": 0.002},
            default="gpt-3.5",
            registered={"gpt-4", "gpt-3.5"},
        )
        effect = Invoke(port="llm", payload={'messages': [], 'task_hint': 'code', 'meta': {'max_cost_per_call': 0.01}})
        assert router.route(effect, pctx()).model == "gpt-3.5"

    def test_unregistered_target_rejected(self) -> None:
        """路由目标未注册：装配期/运行期拒绝（可插拔契约校验）。"""
        import pytest
        with pytest.raises(ValueError, match="registered"):
            RuleRouter(routes={"code": "ghost"}, registered={"gpt-4"})

    def test_decision_carries_reason_for_trace(self) -> None:
        """路由决策带 reason，供 trace 记录。"""
        router = RuleRouter(routes={"code": "gpt-4"}, registered={"gpt-4"})
        effect = Invoke(port="llm", payload={'messages': [], 'task_hint': 'code'})
        d = router.route(effect, pctx())
        assert isinstance(d, RouteDecision)
        assert d.reason  # 非空，进 trace
