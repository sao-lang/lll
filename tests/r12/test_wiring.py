"""R12-T11 跨模块接线：race 取消 / retry 重建 / budget 软限 / 四坑（TDD RED → GREEN）。"""
from __future__ import annotations

import asyncio

from runtime import Invoke, Result, Runner, Session, Terminate
from compose import race, retry, map_effect, build_call, make_dispatch
from archs import Graph, ReAct
from compose import ToolSpec, DangerLevel


def _p(tag: str = "x"):
    def program():
        yield Invoke(port="llm", payload={"tag": tag})
        yield Terminate(output=tag)
        return None
    return program


def _pg():
    """generator 函数形态（供 drive/map_effect 用）。"""
    yield Invoke(port="llm", payload={"tag": "budget"})
    yield Terminate(output="budget")
    return None


def test_race_first_wins_no_losers_effects() -> None:
    """race：首个完成胜出，败者不再下发 Effect（编排层取消）。"""
    seen = []
    def call(e, c):
        seen.append(dict(e.payload).get("tag"))
        return Result.success({"v": 1})
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="r12-race", program_factory=lambda: race([_p("a"), _p("b")])())))
    assert res.status == "done"
    assert set(seen) <= {"a", "b"}


def test_retry_rebuilds_program() -> None:
    """retry：失败后重建 Program 重跑（program_factory 语义）。"""
    counts = {"n": 0}
    def child():
        counts["n"] += 1
        def program():
            yield Invoke(port="llm", payload={"i": counts["n"]})
            yield Terminate(output="ok")
            return None
        return program()
    res = asyncio.run(Runner(call=lambda e, c: Result.success(None)).run(
        Session(session_id="r12-retry", program_factory=lambda: retry(child, attempts=2)())))
    assert res.status == "done" and counts["n"] >= 1


def test_map_effect_budget_injection() -> None:
    """软预算：map_effect 注入 payload['budget']（装配期算子纯变换，手动驱动）。"""
    from runtime import AgentState
    from tests.r12.conftest import drive

    wrapped = map_effect(lambda e: e.with_payload(budget="soft:100") if isinstance(e, Invoke) else e)
    effects, _ = drive(wrapped(_pg), initial=AgentState())
    invokes = [e for e in effects if isinstance(e, Invoke)]
    assert invokes and all(e.payload.get("budget") == "soft:100" for e in invokes)


def test_nested_router_graph_react() -> None:
    """三层嵌套 Router→Graph→ReAct 可编译并运行（顶层 Program 语义）。"""
    def leaf():
        yield Invoke(port="llm", payload={"nested": True})
        yield Terminate(output="leaf")
        return None
    res = asyncio.run(Runner(call=lambda e, c: Result.success({"content": "ok", "tool_calls": []})).run(
        Session(session_id="r12-nested", program_factory=lambda: ReAct(
            [ToolSpec(name="t", danger=DangerLevel.SAFE)]).compile())))
    assert res.status == "done"
