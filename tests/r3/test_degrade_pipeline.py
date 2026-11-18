"""R3 降级链路领域逻辑（ARCH-v5）：逐级收缩选级 → 末级 REFUSE。

守卫故障 → scope.emit(Signal("degrade", reason, failed_target))，
Degradation.select(signals) 从 available 剔除 failed_target 后按 ladder 选下一级；
全部目标失败则落 REFUSE。端到端链路由 tests/r2/test_v5_wiring.py 与
tests/r4/test_v5_e2e.py 覆盖，这里只测选级领域逻辑本身。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, DecisionAction, Host, RequestScope, Signal

from harness.resilience import DegradeLadder, DegradeMode, DegradeStep, Degradation


def _ladder() -> DegradeLadder:
    """四级阶梯：主 → 备 → 缓存 → 拒答（quality 严格递减）。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.7),
        DegradeStep(target="cache", mode=DegradeMode.CACHE, quality=0.4),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _policy() -> Degradation:
    return Degradation(_ladder(), available={"primary", "backup", "cache"})


class TestProgressiveDegradation:
    def test_available_shrinks_level_by_level(self) -> None:
        """每失败一个目标就收缩 available，选中下一质量级。"""
        plugin = _policy()
        assert plugin.select([]) == "primary"  # 健康：最高质量级

        primary_down = Signal("degrade", "breaker_open", "primary")
        assert plugin.select([primary_down]) == "backup"

        backup_down = Signal("degrade", "timeout", "backup")
        assert plugin.select([primary_down, backup_down]) == "cache"

        cache_down = Signal("degrade", "cache_miss", "cache")
        assert plugin.select([primary_down, backup_down, cache_down]) == DegradeMode.REFUSE.value

    def test_failed_target_not_registered_is_noop(self) -> None:
        """失败目标不在 available 时选级不变（不误伤健康目标）。"""
        plugin = _policy()
        assert plugin.select([Signal("degrade", "ghost_down", "ghost")]) == "primary"

    def test_only_degrade_signals_count(self) -> None:
        """abort / budget 等其他信号不参与降级选级。"""
        plugin = _policy()
        signals = [
            Signal("abort", "manual"),
            Signal("budget", "exhausted"),
        ]
        assert plugin.select(signals) == "primary"

    def test_pick_uses_passed_available_not_stored(self) -> None:
        """ladder.pick 以传入的 available 为准，不读插件自身状态。"""
        plugin = _policy()
        assert plugin.pick({"primary", "backup", "cache"}).target == "primary"
        assert plugin.pick(set()).mode is DegradeMode.REFUSE

    def test_empty_available_starts_at_refuse(self) -> None:
        """装配时无可用目标 → 直接 REFUSE（不编造后端）。"""
        plugin = Degradation(_ladder(), available=set())
        assert plugin.select([]) == DegradeMode.REFUSE.value


class TestDegradationPluginSettle:
    def test_settle_selects_target_from_signals(self) -> None:
        """Degradation 插件 tap settle：读 scope.signals，由插件自身选级。"""
        async def run() -> None:
            host = Host()
            degradation = Degradation(_ladder(), available={"primary", "backup", "cache"})
            host.plugin(degradation).freeze()
            assert host.hook("settle").names == ("r3_degrade",)

            scope = host.new_scope()
            scope.emit(Signal("degrade", "breaker_open", "primary"))
            d: Decision | None = await host.hook("settle").call(scope)
            assert d is not None and d.action is DecisionAction.DEGRADE
            assert d.target == "backup"

        asyncio.run(run())

    def test_settle_noop_without_degrade_signal(self) -> None:
        """无 degrade 信号时 settle 不产出降级判决。"""
        async def run() -> None:
            host = Host()
            host.plugin(Degradation(_ladder(), available={"primary", "backup"})).freeze()
            scope = RequestScope()
            scope.emit(Signal("abort", "manual"))
            assert await host.hook("settle").call(scope) is None

        asyncio.run(run())
