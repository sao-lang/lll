"""R11-T01 A3 装配器：profile 预设 + capability 装配（TDD RED → GREEN）。"""
from __future__ import annotations

import pytest

from api import AgentBuilder, PROFILES, profile_names
from archs import ReAct
from compose import Agent, ToolSpec, DangerLevel
from tests.r11.conftest import StubArch


def test_profile_names_three_tiers() -> None:
    """profile 预设三档 dev/production/regulated 齐全。"""
    assert set(profile_names()) == {"dev", "production", "regulated"}


def test_profile_applies_capabilities() -> None:
    """profile('production') 装配可靠性+RAG+安全+OTel 默认能力（记录到 builder 清单）。"""
    b = AgentBuilder(StubArch()).profile("production")
    assert "production" in b.applied_profiles()
    caps = b.capability_names()
    assert "security" in caps and "resilience" in caps and "observability" in caps


def test_profile_regulated_forces_audit() -> None:
    """regulated 档强制审计+审批+隔离。"""
    b = AgentBuilder(StubArch()).profile("regulated")
    caps = b.capability_names()
    assert "security" in caps and "audit" in caps


def test_unknown_profile_rejected() -> None:
    """未知 profile 装配期拒绝（不静默忽略）。"""
    with pytest.raises(ValueError):
        AgentBuilder(StubArch()).profile("unknown")


def test_builder_build_returns_session() -> None:
    """builder.build() 产可跑 Session（委托 R12 Agent）。"""
    session = AgentBuilder(StubArch()).build()
    assert session.program_factory() is not None


def test_r9_ports_default_assembly() -> None:
    """功能接线落地：production profile 装配 R9 三 Port 到 ToolsCapability（组合层，非 R5 改动）。"""
    b = AgentBuilder(StubArch()).profile("production")
    reg = b.registry()
    assert reg.has("sandbox") and reg.has("audit") and reg.has("human")
