"""RED：读不存在 port="state" 的 4 个算子必须从公开 API 移除，回注值驱动的替代算子须真可用。

裂缝：branch/loop/map_state/gate 首 yield Invoke(port="state")，真实 Runner 的
dispatch 只注册 llm/tool/skill/rag/memory（PortName 具名取值域），未注册 state →
Result.failure(PORT_MISSING) → _as_state(失败 Result) 得空 AgentState → 业务走错
分支且无任何 error，run 返回 done + 错误 output。tests/r12/conftest.py 的 drive()
专门把 state 探针回注成初始 State，正好掩盖了该 bug。

修复方向（基于源码判断）：R1 的 Program 只从 send(Result.data) 读数据，
program_factory 无参、ctx 只流向 call，框架没有任何合法 State 通道；
这 4 个算子在 archs 中零使用（已被 loop_until/branch_value 取代），故删干净，
并把真正可用的替代算子公开。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import api.dsl as dsl
import compose
import compose.program as program
from compose.dispatch import make_dispatch
from runtime import Invoke, Program, Result, Runner, Session, Terminate

_STATE_PORT_OPS = ("branch", "loop", "map_state", "gate")


def test_broken_state_port_operators_are_not_public() -> None:
    """4 个 state 探针算子不得残留在任何公开导出面（死代码零残留）。"""
    for name in _STATE_PORT_OPS:
        assert name not in program.__all__, f"program.__all__ 仍导出 {name}"
        assert not hasattr(program, name), f"compose.program 仍有 {name}"
        assert name not in compose.__all__, f"compose.__all__ 仍导出 {name}"
        assert not hasattr(compose, name), f"compose 仍暴露 {name}"
        assert name not in dsl.__all__, f"dsl.__all__ 仍导出 {name}"
        assert not hasattr(dsl, name), f"dsl 仍暴露 {name}"


def test_branch_value_is_public_and_routes_correctly_under_real_runner() -> None:
    """替代算子 branch_value 必须公开，且在真实 Runner 下精确走对 arm、产出其 output。"""
    assert "branch_value" in program.__all__
    assert hasattr(compose, "branch_value") and "branch_value" in compose.__all__
    assert hasattr(dsl, "branch_value") and "branch_value" in dsl.__all__
    assert dsl.branch_value is program.branch_value

    seen: list[str] = []

    def source() -> Program:
        injected = yield Invoke(port="llm", payload={"name": "intent"})
        yield Terminate(output=injected)

    def arm_a() -> Program:
        seen.append("a")
        yield Invoke(port="llm", payload={"name": "a"})
        yield Terminate(output="ARM_A")

    def arm_b() -> Program:
        seen.append("b")
        yield Invoke(port="llm", payload={"name": "b"})
        yield Terminate(output="ARM_B")

    def handler(effect: Invoke, ctx: object) -> Result:
        name = str(effect.payload.get("name"))
        return Result.success({"intent": "b"} if name == "intent" else {"ran": name})

    factory = dsl.branch_value(
        source, lambda value: str(value["intent"]), {"a": arm_a, "b": arm_b}
    )
    result = asyncio.run(Runner(call=make_dispatch({"llm": handler})).run(
        Session(session_id="branch-value-red", program_factory=factory)
    ))
    assert result.status == "done"
    assert result.error is None
    assert seen == ["b"], f"真实 Runner 必须只走 b 臂，实得 {seen}"
    assert result.output == "ARM_B", f"整体产出必须是 b 臂 output，实得 {result.output!r}"


def test_loop_until_is_public_and_loops_under_real_runner() -> None:
    """替代算子 loop_until 必须公开，且在真实 Runner 下按 done 真停止、产出末轮值。"""
    assert "loop_until" in program.__all__
    assert hasattr(compose, "loop_until") and "loop_until" in compose.__all__
    assert hasattr(dsl, "loop_until") and "loop_until" in dsl.__all__

    calls = {"n": 0}

    def handler(effect: Invoke, ctx: object) -> Result:
        calls["n"] += 1
        return Result.success({"n": calls["n"]})

    def step(feedback: object) -> Callable[[], Program]:
        def program_step() -> Program:
            injected = yield Invoke(port="llm", payload={"name": "step"})
            yield Terminate(output=injected["n"])
        return program_step

    factory = dsl.loop_until(step, done=lambda out: out >= 2, max_rounds=3)
    result = asyncio.run(Runner(call=make_dispatch({"llm": handler})).run(
        Session(session_id="loop-until-red", program_factory=factory)
    ))
    assert result.status == "done"
    assert result.error is None
    assert calls["n"] == 2, f"done 命中后必须停止，实际调用 {calls['n']} 次"
    assert result.output == 2, f"产出必须是末轮回注值 2，实得 {result.output!r}"
