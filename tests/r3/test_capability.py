"""R3 能力装配测试：ResilienceCapability.install(container) 按 AOP 角色注册插件。

插件保留一等身份（name + 实例状态 + 构造注入），install 按角色分派到 AOPContainer；
装配顺序即同角色内执行顺序。opt-in：不传即不装。
"""
from __future__ import annotations

import pytest

from compose import AOPContainer
from runtime import Result

from harness.resilience import (
    Bulkhead, CircuitBreaker, DegradeLadder, DegradeMode, DegradeStep,
    Degradation, Idempotency, KillSwitch, RateLimit, ResilienceCapability,
    RetryPolicy, Timeout, Watchdog,
)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _names(plugins: list[object]) -> list[str]:
    return [str(getattr(p, "name", "")) for p in plugins]


class TestCapabilityAssembly:
    def test_no_args_installs_nothing(self) -> None:
        """opt-in：不传插件 → 容器各角色零注册。"""
        cap = ResilienceCapability()
        assert cap.plugins() == ()
        c = cap.install(AOPContainer(lambda inv: Result.success("x")))
        assert c._wardens == [] and c._envelopes == [] and c._refiners == [] and c._salvages == []

    def test_explicit_builtin_registered_by_role(self) -> None:
        """只装传入插件，按角色分组注册。"""
        cap = ResilienceCapability(killswitch=KillSwitch(flag_store={}), retry=RetryPolicy())
        c = cap.install(AOPContainer(lambda inv: Result.success("x")))
        assert _names(c._wardens) == ["r3_killswitch"]
        assert _names(c._envelopes) == ["r3_retry"]
        assert "r3_degrade" not in _names(c._salvages)

    def test_all_plugins_by_role(self) -> None:
        """全插件装配：各角色分组含预期插件，顺序即装配顺序。"""
        cap = ResilienceCapability(
            killswitch=KillSwitch(flag_store={}), watchdog=Watchdog(),
            idempotency=Idempotency(), bulkhead=Bulkhead(), rate_limit=RateLimit(),
            breaker=CircuitBreaker(), retry=RetryPolicy(), timeout=Timeout(),
            degradation=Degradation(_ladder()),
        )
        c = AOPContainer(lambda inv: Result.success("x"))
        assert cap.install(c) is c  # 链式返回同一容器
        w = _names(c._wardens)
        assert "r3_killswitch" in w and "r3_watchdog" in w and "r3_idempotency" in w
        assert "r3_ratelimit" in w and "r3_breaker" in w
        assert set(_names(c._envelopes)) == {"r3_bulkhead", "r3_breaker", "r3_retry", "r3_timeout"}
        # BudgetGuard 已删除（规格 P3）：软限由 A 类 map_effect 注入，硬限在 R1；
        # call 装配器不再有 refiner 角色承载预算策略。
        assert _names(c._refiners) == []
        assert _names(c._salvages) == ["r3_degrade"]
        # CircuitBreaker 拆 check(Warden) + observe(Envelope) 两角色，仍算一个插件。

    def test_warden_chain_blocks_via_verdict(self) -> None:
        """killswitch 置位时 Warden.guard 返回 allowed=False。"""
        import asyncio
        from compose.context import Invocation
        from runtime import Invoke
        kill = KillSwitch(flag_store={})
        kill.flag_store["kill:session"] = True
        kill.flag_store["kill:global"] = True  # 用 global 确保命中（session scope 键为 kill:session 也行）
        from tests.r3.conftest import make_ctx
        inv = make_ctx(Invoke(port="llm", payload={}))
        verdict = asyncio.run(kill.guard(inv))
        assert verdict.allowed is False and verdict.reason == "killswitch"


class TestCustomPlugins:
    def test_custom_degradation_injected(self) -> None:
        """注入自定义 Degradation 实例： Salvage 用其插件名。"""
        class CustomDegradation(Degradation):
            name = "custom_degrade"
        custom = CustomDegradation(_ladder())
        c = ResilienceCapability(degradation=custom).install(AOPContainer(lambda inv: Result.success("x")))
        assert _names(c._salvages) == ["custom_degrade"]

    def test_no_args_installs_no_defaults(self) -> None:
        """不传即空：不隐式装默认插件。"""
        assert ResilienceCapability().plugins() == ()


class TestExtraPlugins:
    def test_extra_warden_reaches_role(self) -> None:
        """extra_plugins 注入 Warden 角色实例：install 后进入 warden 分组。"""
        from compose.context import Verdict
        from compose import Warden
        class AuditWarden(Warden):
            name = "custom_audit"
            async def guard(self, inv): return Verdict(allowed=True)
        c = ResilienceCapability(extra_plugins=(AuditWarden(),)).install(
            AOPContainer(lambda inv: Result.success("x"))
        )
        assert "custom_audit" in _names(c._wardens)

    def test_extra_plugin_in_plugins(self) -> None:
        """extra_plugins 原样进 plugins()。"""
        from compose.context import Verdict
        from compose import Warden
        class AuditWarden(Warden):
            name = "custom_audit"
            async def guard(self, inv): return Verdict(allowed=True)
        custom = AuditWarden()
        assert custom in ResilienceCapability(extra_plugins=(custom,)).plugins()

    def test_extra_plugin_without_name_rejected(self) -> None:
        """履约校验：无 name 的插件在装配期被拒绝。"""
        class NoName:
            pass
        with pytest.raises(ValueError, match="name"):
            ResilienceCapability(extra_plugins=(NoName(),))  # type: ignore[arg-type]
