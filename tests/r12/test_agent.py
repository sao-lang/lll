"""R12-T11/T09 用户入口 Agent 装配器（TDD RED → GREEN）。

验证：capability 装配 / rag/memory/telemetry/event_stream 注入 / build 产 Session /
static_check 拒错 / version 进 State/trace / 换实现上层零改。
"""
from __future__ import annotations

import pytest

from compose import Agent, Registry
from archs.base import StaticCheckReport, StaticCheckIssue
from runtime import AgentState, Session, Terminate


class _Arch:
    """最小 Arch：compile 产 Terminate(output=tag)。"""
    name = "test_arch"
    version = "12.0.0"

    def __init__(self, tag: str = "ok", errors: tuple[StaticCheckIssue, ...] = ()) -> None:
        self.tag = tag
        self._errors = errors

    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(errors=self._errors, arch=self.name, version=self.version,
                                 ports_required=(), budgets_resolved={})

    def compile(self):
        def program():
            yield Terminate(output=self.tag)
            return None
        return program()


class _Cap:
    """最小 Capability：install 注册一个 provider。"""
    def __init__(self, name: str = "fake_cap", port: str = "tool") -> None:
        self.name = name
        self.version = "1.0"
        self.requires: tuple[str, ...] = ()
        self.port = port

    def install(self, container: object, reg: Registry) -> None:
        reg.register(self.port, object())


def test_agent_build_produces_session() -> None:
    """Agent(arch).build() 产可跑 Session（用户唯一入口）。"""
    agent = Agent(_Arch("hello"))
    session = agent.build()
    assert isinstance(session, Session)
    assert session.program_factory() is not None
    assert session.version  # 版本已登记（trace 对齐）


def test_agent_capability_chain() -> None:
    """capability() 链式装配，install 注册进共用 Registry。"""
    agent = Agent(_Arch())
    out = agent.capability(_Cap("c1", "extra1"))
    assert out is agent  # Self 链式
    reg = agent.registry()
    assert reg.has("extra1")


def test_agent_version_into_state() -> None:
    """build 后 State.version 含架构名+版本（回放可对齐）。"""
    agent = Agent(_Arch("v"))
    session = agent.build()
    assert "test_arch" in session.state.version


def test_agent_rejects_static_errors() -> None:
    """static_check 有 error 时 build 拒绝（装配期挡错，不进运行期）。"""
    bad = _Arch(errors=(StaticCheckIssue("R12-E11", "缺 port", "llm"),))
    with pytest.raises(ValueError, match="R12-E11"):
        Agent(bad).build()


def test_agent_dead_injection_fields_removed() -> None:
    """死注入字段删除（体检 B1）：rag/memory/telemetry/event_stream 从不进 build，已删。"""
    agent = Agent(_Arch("x"))
    for dead in ("rag", "memory", "telemetry", "event_stream"):
        assert not hasattr(agent, dead), f"死注入方法 {dead} 应删除"
    assert agent.build().program_factory() is not None
