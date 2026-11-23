"""R11-T06 治理：killswitch/成本限额/灰度/feature flag（TDD RED → GREEN）。"""
from __future__ import annotations

from api import Governance, KillSwitch, CostLimiter, Rollout, FeatureFlags


def test_killswitch_blocks_all() -> None:
    """killswitch 开 → 全阻断；关 → 放行（真实状态机）。"""
    ks = KillSwitch()
    assert ks.allow("tool.call")
    ks.enable("maintenance")
    assert not ks.allow("tool.call")
    ks.disable()
    assert ks.allow("tool.call")


def test_cost_limiter_enforces_cap() -> None:
    """成本限额：超 cap 后拒绝，返回剩余额度。"""
    limiter = CostLimiter(cap=1.0)
    assert limiter.charge(0.6)
    assert limiter.charge(0.3)
    assert not limiter.charge(0.2)  # 累计 1.1 > 1.0
    assert limiter.remaining() >= 0.0


def test_rollout_gray_release() -> None:
    """灰度：按 key 哈希分桶，稳定且比例可控。"""
    rollout = Rollout(percent=50)
    hits = sum(1 for i in range(100) if rollout.enabled(f"user-{i}"))
    assert 0 < hits < 100  # 有些进有些不进，非全量非全拒
    assert rollout.enabled("user-7") == rollout.enabled("user-7")  # 确定性


def test_feature_flags() -> None:
    """feature flag：默认关，可打开/关闭（真实状态）。"""
    flags = FeatureFlags()
    assert not flags.is_on("new_ui")
    flags.set("new_ui", True)
    assert flags.is_on("new_ui")
    flags.set("new_ui", False)
    assert not flags.is_on("new_ui")


def test_governance_aggregates() -> None:
    """Governance 聚合四件，统一 allow 判定。"""
    gov = Governance(cost_cap=1.0)
    assert gov.allow("tool.call", cost=0.1, key="u1")
    gov.killswitch.enable()
    assert not gov.allow("tool.call", cost=0.1, key="u1")


def test_governance_gates_builder() -> None:
    """治理真接入装配路径：killswitch 开时 build 拒绝（非 cosmetic）。"""
    import pytest

    from api import AgentBuilder
    from tests.r11.conftest import StubArch

    gov = Governance()
    builder = AgentBuilder(StubArch()).govern(gov)
    assert builder.build().program_factory() is not None
    gov.killswitch.enable("maintenance")
    with pytest.raises(ValueError, match="governance denied"):
        builder.build()
