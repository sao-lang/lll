"""R3 能力装配测试（ARCH-v5）：ResilienceCapability.install(host) 把插件 tap 到宿主扩展点。

v5 契约：插件只实现 apply(host)，不自声明 mode/priority/events；装配顺序即
宿主扩展点内的 tap 顺序。断言对象是 host 的 hook().names，
不再检查旧的 hooks() 排序 / HookMode / priority。
"""
from __future__ import annotations

import asyncio

import pytest

from hooks import DecisionAction, Host, RequestScope

from harness.resilience import (
    BudgetGuard,
    Bulkhead,
    CircuitBreaker,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    Degradation,
    Idempotency,
    KillSwitch,
    RateLimit,
    ResilienceCapability,
    RetryPolicy,
    Timeout,
    Watchdog,
)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


class TestCapabilityAssembly:
    def test_no_args_installs_nothing(self) -> None:
        """opt-in 装配：不传任何插件 → 所有扩展点零 tap。"""
        cap = ResilienceCapability()
        assert cap.plugins() == ()
        host = cap.install(Host())
        for name in ("before_invoke", "transform", "around_invoke",
                     "after_invoke", "invoke_error", "settle"):
            assert host.hook(name).names == ()

    def test_explicit_builtin_instances_tapped_by_name(self) -> None:
        """只装传入的插件，host 对应扩展点 tap 对应插件名。"""
        cap = ResilienceCapability(killswitch=KillSwitch(flag_store={}), retry=RetryPolicy())
        host = cap.install(Host())
        assert "r3_killswitch" in host.hook("before_invoke").names
        assert "r3_retry" in host.hook("around_invoke").names
        assert "r3_breaker" not in host.hook("before_invoke").names  # 没传就不装
        assert "r3_degrade" not in host.hook("settle").names

    def test_all_extension_points_tapped(self) -> None:
        """全插件装配：每个扩展点 tap 到预期插件名，顺序即装配顺序。"""
        cap = ResilienceCapability(
            killswitch=KillSwitch(flag_store={}),
            watchdog=Watchdog(),
            idempotency=Idempotency(),
            bulkhead=Bulkhead(),
            rate_limit=RateLimit(),
            breaker=CircuitBreaker(),
            retry=RetryPolicy(),
            timeout=Timeout(),
            degradation=Degradation(_ladder()),
            budget_guard=BudgetGuard(),
        )
        host = Host()
        assert cap.install(host) is host  # 链式返回同一 host
        assert set(host.hook("before_invoke").names) == {
            "r3_killswitch", "r3_watchdog", "r3_idempotency",
            "r3_bulkhead", "r3_ratelimit", "r3_breaker",
        }
        assert host.hook("around_invoke").names == ("r3_retry", "r3_timeout")
        assert host.hook("settle").names == ("r3_degrade",)
        assert host.hook("transform").names == ("r3_budgetguard",)
        assert "r3_breaker" in host.hook("after_invoke").names
        assert "r3_breaker" in host.hook("invoke_error").names
        host.freeze()  # 装配完整，冻结不抛异常

    def test_guard_chain_short_circuit_trace(self) -> None:
        """killswitch 置位时 before_invoke 的 BAIL tap 直接 ABORT。"""
        kill = KillSwitch(flag_store={})
        host = ResilienceCapability(killswitch=kill).install(Host()).freeze()
        scope = host.new_scope()
        assert asyncio.run(host.hook("before_invoke").call(scope)) is None  # 健康放行
        kill.flag_store["kill:session"] = True
        d = asyncio.run(host.hook("before_invoke").call(scope))
        assert d is not None and d.action is DecisionAction.ABORT


class TestCustomPlugins:
    def test_custom_degradation_injected(self) -> None:
        """注入自定义 Degradation 实例：tap 用其插件名。"""
        class CustomDegradation(Degradation):
            name = "custom_degrade"

        custom = CustomDegradation(_ladder())
        host = ResilienceCapability(degradation=custom).install(Host())
        assert host.hook("settle").names == ("custom_degrade",)

    def test_custom_budget_guard_injected(self) -> None:
        """注入自定义 BudgetGuard（如改 soft 阈值）：tap 到 transform。"""
        custom = BudgetGuard(soft=0.5)
        host = ResilienceCapability(budget_guard=custom).install(Host())
        assert host.hook("transform").names == ("r3_budgetguard",)
        assert custom in ResilienceCapability(budget_guard=custom).plugins()

    def test_no_args_installs_no_defaults(self) -> None:
        """不传即空：不隐式装默认插件。"""
        assert ResilienceCapability().plugins() == ()


class TestExtraPlugins:
    class _CustomPlugin:
        name = "custom_audit"

        def apply(self, host: Host) -> None:
            host.hook("before_invoke").tap(self.name, self.fn)

        async def fn(self, scope: RequestScope) -> None:
            return None

    def test_extra_plugin_reaches_host(self) -> None:
        """extra_plugins 注入任意插件：install 后 tap 到对应扩展点。"""
        host = ResilienceCapability(extra_plugins=(self._CustomPlugin(),)).install(Host())
        assert "custom_audit" in host.hook("before_invoke").names

    def test_extra_plugin_in_plugins(self) -> None:
        """extra_plugins 原样进 plugins()。"""
        custom = self._CustomPlugin()
        assert custom in ResilienceCapability(extra_plugins=(custom,)).plugins()

    def test_extra_plugin_without_name_rejected(self) -> None:
        """履约校验：无 name 的插件在装配期被拒绝。"""
        class NoName:
            def apply(self, host: Host) -> None:
                return None

        with pytest.raises(ValueError, match="name"):
            ResilienceCapability(extra_plugins=(NoName(),))
