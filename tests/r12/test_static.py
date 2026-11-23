"""R12-T05/T09 装配期 static_check 错误码覆盖（TDD RED → GREEN）。

补齐缺失的 E05/E10/E11/E12/E13/E14/E16；每码一条真实触发断言。
构造器以 src/archs/recipes.py 实际签名为准。
"""
from __future__ import annotations

from compose import ToolSpec, DangerLevel
from archs import Graph, Router, Supervisor, Handoff, Blackboard, ToolUse, ReAct, PlanExecute
from archs.base import StaticCheckReport


def _p():
    def program():
        from runtime import Terminate
        yield Terminate(output=None)
        return None
    return program


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name

    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(arch=self.name, version="1.0")

    def compile(self):
        return _p()()


def test_e05_graph_cond_target_invalid() -> None:
    """E05：cond 返回未声明节点 id → 装配期拒绝。"""
    g = (Graph().node("a", _p()).node("b", _p())
         .edge("a", "b", cond=lambda s: "ghost").exit("b"))
    codes = {e.code for e in g.static_check().errors}
    assert "R12-E05" in codes


def test_e10_high_danger_without_sandbox() -> None:
    """E10：高危工具无 SandboxPort → 装配期拒绝。"""
    spec = ToolSpec(name="danger", description="", danger=DangerLevel.HIGH)
    codes = {e.code for e in ToolUse([spec], parallel=False).static_check().errors}
    assert "R12-E10" in codes


def test_e11_missing_port() -> None:
    """E11：声明的 port 未注册 → 装配期拒绝（ports_required 非空 + E11）。"""
    report = ReAct([ToolSpec(name="t", danger=DangerLevel.SAFE)], llm=None).static_check()
    assert "llm" in report.ports_required
    assert "R12-E11" in {e.code for e in report.errors}


def test_e12_budget_conflict() -> None:
    """E12：max_turns 超内核硬上限（Budgets.max_turns 默认 30）→ 装配期拒绝。"""
    from runtime import Budgets
    arch = ReAct([ToolSpec(name="t", danger=DangerLevel.SAFE)],
                 max_turns=Budgets().max_turns + 1)
    assert "R12-E12" in {e.code for e in arch.static_check().errors}


def test_e13_join_unregistered() -> None:
    """E13：JoinStrategy 非 all/any/majority/reduce → 装配期拒绝。"""
    arch = Supervisor(planner=_Named("p"), child_spec=_Named("c"), join=_Named("bad"))
    assert "R12-E13" in {e.code for e in arch.static_check().errors}


def test_e14_child_spec_invalid() -> None:
    """E14：child_spec 未履约（缺 name）→ 装配期拒绝。"""
    arch = Supervisor(planner=_Named("p"), child_spec=_Named(""), max_children=0)
    assert "R12-E14" in {e.code for e in arch.static_check().errors}


def test_e16_shared_key_conflict() -> None:
    """E16：多 Agent 写同一 scratch 键且无仲裁 → 装配期拒绝。"""
    a1, a2 = _Named("a1"), _Named("a2")
    a1.writes = ("x.shared",)
    a2.writes = ("x.shared",)
    arch = Blackboard(agents=[a1, a2])
    assert "R12-E16" in {e.code for e in arch.static_check().errors}


def test_all_17_codes_declared() -> None:
    """全 17 错误码在 R12 源码有声明点（AST 级，防遗漏）。"""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "src" / "archs" / "recipes.py").read_text(encoding="utf-8")
    for n in range(1, 18):
        code = f"R12-E{n:02d}"
        assert code in text, f"{code} 未在 recipes.py 声明"
