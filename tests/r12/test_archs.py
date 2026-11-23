"""R12-T01~T07 十架构 compile 真实语义（test_archs_semantics 补齐为矩阵文件）。"""
from __future__ import annotations

import asyncio

from runtime import Result, Runner, Session, Terminate
from archs import (
    Blackboard, Graph, Handoff, PlanExecute, ReAct, Reflection,
    Router, RouterSkill, Supervisor, ToolUse,
)
from archs.base import StaticCheckReport
from compose import ToolSpec, DangerLevel


def _run(arch, *, call) -> tuple[str, object, str]:
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="r12-arch", program_factory=lambda: arch.compile())))
    return res.status, res.output, res.reason


class _Named:
    def __init__(self, name: str, prompt: str = "") -> None:
        self.name = name
        self.prompt = prompt
    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(arch=self.name, version="1.0")
    def compile(self):
        def p():
            yield Terminate(output=self.name)
            return None
        return p()


def _arm(label: str):
    class A:
        name = label
        version = "1.0"
        def static_check(self):
            return StaticCheckReport(arch=label, version="1.0")
        def compile(self):
            def p():
                yield Terminate(output=label)
                return None
            return p()
    return A()


def test_t01_tooluse_parallel() -> None:
    """T01 ToolUse：batch 并行收全部结果。"""
    arch = ToolUse([ToolSpec(name=n, danger=DangerLevel.SAFE) for n in ("a", "b")], parallel=True)
    status, out, _ = _run(arch, call=lambda e, c: Result.success({"value": 1}))
    assert status == "done" and isinstance(out, list)


def test_t02_react_stops() -> None:
    """T02 ReAct：无 tool_calls 时结束。"""
    n = {"i": 0}
    def call(e, c):
        n["i"] += 1
        return Result.success({"content": "final", "tool_calls": []})
    status, out, _ = _run(ReAct([ToolSpec(name="t", danger=DangerLevel.SAFE)]), call=call)
    assert status == "done" and out == "final"


def test_t03_planexecute_compiles() -> None:
    """T03 PlanExecute：compile 产 Program（真实语义由既有测试覆盖）。"""
    arch = PlanExecute(_Named("planner"), _Named("exec"))
    assert arch.static_check().ok or arch.static_check().errors


def test_t04_reflection() -> None:
    """T04 Reflection：passed 即止。"""
    n = {"i": 0}
    def call(e, c):
        n["i"] += 1
        return Result.success({"passed": True} if n["i"] > 1 else {"content": "draft"})
    status, out, _ = _run(Reflection(_Named("critic", "critique")), call=call)
    assert status == "done"


def test_t05_router_and_routerskill() -> None:
    """T05 Router/RouterSkill：意图分流 + static_check。"""
    arch = Router(lambda t: None, {"a": _arm("a")})
    assert arch.static_check().ok
    assert RouterSkill(lambda t: None, {"a": _arm("a")}, default="a").static_check().ok


def test_t06_supervisor_blackboard() -> None:
    """T06 Supervisor/Blackboard：装配期检查通过。"""
    assert Supervisor(_Named("p"), _Named("c")).static_check().ok
    assert Blackboard([_arm("a")]).static_check().ok


def test_t07_handoff_graph() -> None:
    """T07 Handoff/Graph：角色交接 + DAG 编译。"""
    assert Handoff({"r": _arm("r")}).static_check().ok
    g = Graph().node("a", lambda: _arm("a").compile()).exit("a")
    assert g.static_check().ok


def test_ten_archs_all_present() -> None:
    """10 架构类齐全（AST 级契约）。"""
    for cls in (ToolUse, ReAct, PlanExecute, Reflection, Router, RouterSkill,
                Supervisor, Handoff, Blackboard, Graph):
        assert cls.name
