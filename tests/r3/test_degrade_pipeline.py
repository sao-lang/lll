"""R3 降级链路领域逻辑：逐级收缩选级 → 末级 REFUSE。

具名故障记录（FaultRecord，含 failed_target）由领域策略/执行器写入 inv.meta，
Degradation.select(fault, session_id) 从本会话 available 剔除 failed_target 后按
ladder 选下一级；全部目标失败则落 REFUSE。断言强度不降，只换 API 形态。
"""
from __future__ import annotations

from compose.context import FaultRecord, Invocation
from runtime import Invoke, Result

from harness.resilience import DegradeLadder, DegradeMode, DegradeStep, Degradation
from tests.r3.conftest import make_ctx

SID = "s-pipeline"


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


def _fault(reason: str, target: str) -> FaultRecord:
    return FaultRecord(reason=reason, failed_target=target)


class TestProgressiveDegradation:
    def test_available_shrinks_level_by_level(self) -> None:
        """每失败一个目标就收缩 available，选中下一质量级。"""
        plugin = _policy()
        assert plugin.select(FaultRecord("none", ""), session_id=SID) == "primary"
        assert plugin.select(_fault("breaker_open", "primary"), session_id=SID) == "backup"
        assert plugin.select(_fault("timeout", "backup"), session_id=SID) == "cache"
        assert plugin.select(_fault("cache_miss", "cache"), session_id=SID) == DegradeMode.REFUSE.value

    def test_available_shrinks_across_effects_reaches_refuse(self) -> None:
        """R3-6：跨 Effect 逐级降级，backup 失败不得选回 primary（写回实例）。"""
        plugin = _policy()
        # 第 1 个 Effect：primary 故障 → backup
        assert plugin.select(_fault("500", "primary"), session_id=SID) == "backup"
        # 第 2 个 Effect：backup 故障 → cache（不得选回 primary）
        assert plugin.select(_fault("500", "backup"), session_id=SID) == "cache"
        # 第 3 个 Effect：cache 也故障 → REFUSE
        assert plugin.select(_fault("500", "cache"), session_id=SID) == DegradeMode.REFUSE.value

    def test_failed_target_not_registered_is_noop(self) -> None:
        """失败目标不在 available 时选级不变（不误伤健康目标）。"""
        plugin = _policy()
        assert plugin.select(_fault("ghost_down", "ghost"), session_id=SID) == "primary"

    def test_non_degrade_reason_ignored(self) -> None:
        """非降级原因（abort/budget）且无 failed_target 时不参与选级。"""
        plugin = _policy()
        assert plugin.select(FaultRecord("abort", ""), session_id=SID) == "primary"
        assert plugin.select(FaultRecord("budget_exhausted", ""), session_id=SID) == "primary"

    def test_pick_uses_passed_available_not_stored(self) -> None:
        """ladder.pick 以传入的 available 为准。"""
        plugin = _policy()
        assert plugin.pick({"primary", "backup", "cache"}).target == "primary"
        assert plugin.pick(set()).mode is DegradeMode.REFUSE

    def test_empty_available_starts_at_refuse(self) -> None:
        """装配时无可用目标 → 直接 REFUSE（不编造后端）。"""
        plugin = Degradation(_ladder(), available=set())
        assert plugin.select(FaultRecord("none", ""), session_id=SID) == DegradeMode.REFUSE.value


class TestDegradationRecovery:
    def test_recovery_writes_typed_routing_target(self) -> None:
        """执行器读 fault → select → 写 typed routing_target 供网关消费。"""
        import asyncio

        from harness.resilience import ResilienceCapability

        plugin = Degradation(_ladder(), available={"primary", "backup", "cache"})
        inv = make_ctx(Invoke(port="llm", payload={"model": "primary"}))
        inv.write_fault(_fault("breaker_open", "primary"))
        calls: list[str] = []

        async def core(i: Invocation) -> object:
            rt = i.routing_target()
            calls.append(rt.target if rt else "primary")
            return Result.success("backup-answer")

        executor = ResilienceCapability(degradation=plugin).wrap(core)
        result = asyncio.run(executor.run_invocation(inv))
        assert result.ok
        rt = inv.routing_target()
        assert rt is not None and rt.target == "backup"  # primary 剔除后选 backup
        assert rt.reason == "breaker_open"
        assert calls == ["backup"]
