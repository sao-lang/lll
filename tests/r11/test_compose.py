"""R11-T02 A2 架构组合：组合算子+嵌套经 builder 暴露（TDD RED → GREEN）。"""
from __future__ import annotations

from api import AgentBuilder
from compose import seq, branch, map_state
from runtime import AgentState, Invoke, Terminate
from tests.r11.conftest import StubArch


class _Arch:
    name = "compose_arch"
    version = "1.0.0"

    def static_check(self):
        from archs.base import StaticCheckReport
        return StaticCheckReport(arch=self.name, version=self.version)

    def compile(self):
        def p():
            yield Invoke(port="llm", payload={"n": 1})
            yield Terminate(output="composed")
            return None
        return p()


def test_builder_accepts_composed_arch() -> None:
    """A2：接受任意 compile() 架构（组合算子的结果），零改动。"""
    session = AgentBuilder(_Arch()).build()
    assert session.program_factory() is not None


def test_builder_static_check_reports_errors() -> None:
    """A2：非法架构装配期报错。"""
    class Bad(StubArch):
        def static_check(self):
            from archs.base import StaticCheckReport, StaticCheckIssue
            return StaticCheckReport(errors=(StaticCheckIssue("R12-E11", "缺 port", "llm"),),
                                     arch="bad", version="1.0")
    import pytest
    with pytest.raises(ValueError, match="R12-E11"):
        AgentBuilder(Bad()).build()
