"""R12 内置架构真实语义回归（补全 compile 骨架后）。

原 test_tool_layers 仅验装配形态（leaf 名字/Effect 数量），不验架构行为；
本文件用真实 Runner 驱动，验证 phase 机、反思循环、条件路由、图跳转等语义。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import Invoke, Result, Runner, Session, Terminate
from archs import Graph, IntentResult, ReAct, Reflection, Router, ToolUse
from compose import ToolSpec


def _run(arch, *, call) -> tuple[str, object, str]:
    """用真实 Runner 驱动架构，返回 (status, output, reason)。"""
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="arch-sem", program_factory=lambda: arch.compile())))
    return res.status, res.output, res.reason


def test_tooluse_serial_collects_all_results() -> None:
    """串行工具调用：收集每个工具的回注结果（不丢中间项）。"""
    seen = []

    def call(effect, ctx):
        seen.append(dict(effect.payload))
        return Result.success({"value": effect.payload.get("name")})

    arch = ToolUse([ToolSpec(name="t1"), ToolSpec(name="t2"), ToolSpec(name="t3")], parallel=False)
    status, output, _ = _run(arch, call=call)
    assert status == "done"
    assert output == [{"value": "t1"}, {"value": "t2"}, {"value": "t3"}]
    assert [p.get("name") for p in seen] == ["t1", "t2", "t3"]


def test_react_loops_until_no_tool_calls() -> None:
    """ReAct：有 tool_calls 继续循环，无则输出最终 content。"""
    n = {"i": 0}

    def call(effect, ctx):
        if effect.port == "llm":
            n["i"] += 1
            if n["i"] < 3:
                return Result.success({"content": "thinking", "tool_calls": [
                    {"name": "t", "args": {}}]})
            return Result.success({"content": "final", "tool_calls": []})
        return Result.success({"value": 7})

    arch = ReAct([ToolSpec(name="t")], max_turns=10)
    status, output, _ = _run(arch, call=call)
    assert status == "done"
    assert output == "final"
    assert n["i"] == 3  # 两轮工具 + 一次收尾


def test_react_exhausts_turns_defensively() -> None:
    """ReAct：port 永不收敛时，max_turns 作防御上限退出（reason=turns_exhausted）。"""
    def call(effect, ctx):
        if effect.port == "llm":
            return Result.success({"content": "loop", "tool_calls": [{"name": "t"}]})
        return Result.success({"value": 1})

    arch = ReAct([ToolSpec(name="t")], max_turns=2)
    status, output, reason = _run(arch, call=call)
    assert status == "done" and reason == "turns_exhausted"


def test_reflection_stops_on_passed() -> None:
    """Reflection：critique.passed 为 True 时输出当前 best draft。"""
    n = {"i": 0}

    def call(effect, ctx):
        n["i"] += 1
        seq = {1: {"content": "draft v1"}, 2: {"passed": False},
               3: {"content": "draft v2"}, 4: {"passed": True}}
        return Result.success(seq[n["i"]])

    arch = Reflection(type("R", (), {"name": "critic", "prompt": "critique"})(), max_rounds=5)
    status, output, _ = _run(arch, call=call)
    assert status == "done" and output == "draft v2"


def test_router_selects_arm_by_intent_label() -> None:
    """Router：按 classifier 的 label 选 arm（非固定 default）。"""
    def make_arm(name):
        class A:
            def compile(self):
                def p():
                    yield Terminate(output=name)
                    return None
                return p()
        return A()

    classifier = lambda text: IntentResult(label="b", confidence=0.9)
    arch = Router(classifier, {"a": make_arm("arm-a"), "b": make_arm("arm-b")}, default="a")
    status, output, _ = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done" and output == "arm-b"


def test_router_low_confidence_falls_back_to_default() -> None:
    """Router：置信度不足走 default。"""
    def make_arm(name):
        class A:
            def compile(self):
                def p():
                    yield Terminate(output=name)
                    return None
                return p()
        return A()

    classifier = lambda text: IntentResult(label="b", confidence=0.1)
    arch = Router(classifier, {"a": make_arm("arm-a"), "b": make_arm("arm-b")},
                  default="a", min_confidence=0.5)
    status, output, _ = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done" and output == "arm-a"


def test_graph_conditional_edge_selects_branch() -> None:
    """Graph：cond(state) 返回目标 id，按条件边跳转（非顺序串接）。"""
    seen = []

    def node_program(name):
        def p():
            seen.append(name)
            yield Invoke(port="llm", payload={"name": name})
            yield Terminate(output={"node": name})
            return None
        return p

    graph = (Graph()
             .node("start", node_program("start"))
             .node("left", node_program("left"))
             .node("right", node_program("right"))
             .edge("start", "left", cond=lambda s: "left")
             .edge("start", "right")
             .exit("left"))
    assert graph.static_check().ok
    status, output, _ = _run(graph, call=lambda e, c: Result.success({"v": 1}))
    assert status == "done"
    assert seen == ["start", "left"], f"应走 start→left，实际 {seen}"
    assert output == {"node": "left"}


def test_graph_static_check_detects_cycle_and_unreachable() -> None:
    """Graph static_check：环(E02)、不可达(E03)、多 exit(E04)、cond 目标重复(E06)。"""
    def p():
        yield Terminate(output=None)
        return None

    # 环：a→b→a
    cyclic = Graph().node("a", p).node("b", p).edge("a", "b").edge("b", "a").exit("b")
    codes = {e.code for e in cyclic.static_check().errors}
    assert "R12-E02" in codes

    # 不可达：c 无入边（entry=a，exit=c，a 无出边到 c 的路径）
    unreachable = Graph().node("a", p).node("c", p).exit("c")
    codes = {e.code for e in unreachable.static_check().errors}
    assert "R12-E03" in codes

    # 多 exit
    multi = Graph().node("a", p).exit("a").exit("b")
    codes = {e.code for e in multi.static_check().errors}
    assert "R12-E04" in codes

    # cond 目标重复：同一 src 两条 cond 边都指向 b
    dup = (Graph().node("a", p).node("b", p)
           .edge("a", "b", cond=lambda s: "b").edge("a", "b", cond=lambda s: "b")
           .exit("b"))
    codes = {e.code for e in dup.static_check().errors}
    assert "R12-E06" in codes
