"""R11-T05 装配期检查：缺 port/非法架构/预算冲突报错（TDD RED → GREEN）。"""
from __future__ import annotations

import pytest

from api import AgentBuilder, static_check_api
from archs.base import StaticCheckReport, StaticCheckIssue
from tests.r11.conftest import StubArch


def test_missing_port_reported() -> None:
    """缺 port 经 static_check_api 聚合报告。"""
    class Bad(StubArch):
        def static_check(self):
            return StaticCheckReport(errors=(StaticCheckIssue("R12-E11", "缺 llm", "llm"),),
                                     arch="bad", version="1.0")
    report = static_check_api(Bad())
    assert not report.ok and any(e.code == "R12-E11" for e in report.errors)


def test_budget_conflict_reported() -> None:
    """预算冲突 E12 上报。"""
    class Bad(StubArch):
        def static_check(self):
            return StaticCheckReport(errors=(StaticCheckIssue("R12-E12", "预算冲突", "cost"),),
                                     arch="bad", version="1.0")
    assert "R12-E12" in {e.code for e in static_check_api(Bad()).errors}


def test_valid_arch_ok() -> None:
    """合法架构报告 ok。"""
    assert static_check_api(StubArch()).ok
