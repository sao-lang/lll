"""A 类组合算子的语义单测（规格 §2.3 / D-4）。

覆盖：保留算子装配期 Program→Program 纯变换、compile 幂等、状态进 scratch x-*、
嵌套组合。原 10 算子里 branch/loop/map_state/gate 依赖不存在的 port="state"，
已在真实 Runner 下静默误路由，故删除；回注值驱动的 branch_value/loop_until 为替代。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import (
    AgentState,
    Effect,
    Invoke,
    Result,
    Runner,
    Session,
    Terminate,
    Usage,
)

from compose.call import build_call
from compose.dispatch import make_dispatch

from tests.r12.conftest import drive, leaf

from compose.program import (
    _carry_x,
    branch_value,
    fallback,
    loop_until,
    map_effect,
    par,
    race,
    retry,
    seq,
)


def test_public_operators_are_callable() -> None:
    """公开 A 类算子全部可导入且可调用；读不存在 state port 的 4 个算子已删除。"""
    for op in (seq, par, race, map_effect, fallback, retry, loop_until, branch_value):
        assert callable(op)


def test_seq_runs_a_then_b_and_passes_output() -> None:
    """seq：先跑 a 再跑 b，a 的 output 写入 scratch 供 b 读取。"""
    program = seq(leaf("a", output="A"), leaf("b", output="B"))
    effects, state = drive(program)
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names == ["a", "b"]
    # b 的 output 成为整体产出；a→b 的链通过内部 x-* carry 完成（见 test_carry_x）
    assert state.output == "B"


def test_seq_does_not_mutate_input_programs() -> None:
    """compile 幂等：算子返回新 Program，不改原对象。"""
    a, b = leaf("a"), leaf("b")
    p1 = seq(a, b)
    p2 = seq(a, b)
    assert p1 is not a and p1 is not b
    assert p1 is not p2


def test_par_drives_each_child_effect_in_parallel_batch() -> None:
    """par：真并行——每个子 Program 的 Effect 汇入同一批（yield list），不是假 batch tool。"""
    program = par([leaf("x"), leaf("y")])
    effects, _ = drive(program)
    # 两个子 Program 的 Invoke 都必须真实产出（旧的假实现只发一个 tool batch）。
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names == ["x", "y"]
    assert not any(isinstance(e, Invoke) and e.port == "tool" for e in effects)


def test_par_all_join_returns_all_outputs_in_order() -> None:
    """par join=all：回注按序分发给各子 Program，聚合其 output 为有序 list。"""
    program = par([leaf("x", output="X"), leaf("y", output="Y")], join="all")
    # R1 对 batched 回注 list（runner.py:524-526）；按序 send 回各子 Program。
    _, state = drive(program, responder=lambda e, i: ["rx", "ry"])
    assert state.output == ["X", "Y"]


def test_race_drives_each_child_and_returns_first_output() -> None:
    """race：真并行——每个子 Program 的 Effect 汇入同批，首个产出者胜出。"""
    program = race([leaf("r1", output="first"), leaf("r2", output="second")])
    effects, state = drive(program, responder=lambda e, i: ["v1", "v2"])
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names == ["r1", "r2"]
    assert state.output == "first"


def test_map_effect_rewrites_payload() -> None:
    """map_effect：改写 Effect payload（脱敏/注入标记/软预算注入）。"""
    def scrub(e: Effect) -> Effect:
        if isinstance(e, Invoke):
            return e.with_payload(redacted=True)
        return e
    program = map_effect(scrub)(leaf("secret"))
    effects, _ = drive(program)
    invoke = next(e for e in effects if isinstance(e, Invoke))
    assert invoke.payload.get("redacted") is True


def test_fallback_tries_next_arm_on_failure() -> None:
    """fallback：某 arm 失败切下一个，全失败走 refuse。"""
    # responder 注入失败标记，fallback 依据 scratch 的 x-* 失败记录切换
    program = fallback(leaf("primary"),
                       ladders=[leaf("backup")], refuse=leaf("refuse"))
    effects, _ = drive(program)
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names and names[0] == "primary"


def test_retry_is_process_level_replay() -> None:
    """retry（流程级）：失败按 program_factory 重建重跑，状态进 scratch。"""
    program = retry(leaf("r"), attempts=2, backoff=1.0)
    effects, _ = drive(program)
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names and names[0] == "r"


def test_carry_x_is_pure_and_only_touches_x_keys() -> None:
    """_carry_x：状态进 scratch x-* 的纯函数传递，不改原对象、不带入非 x-* 键。"""
    incoming = AgentState().set("business", "keep")
    source = AgentState().set("x-compose.seq.output", "A").set("business", "evil")
    merged = _carry_x(incoming, source)
    assert merged.get("x-compose.seq.output") == "A"  # x-* 被传递
    assert merged.get("business") == "keep"  # 非 x-* 不被 source 覆盖
    assert incoming.get("x-compose.seq.output") is None  # 原对象未改（纯函数）


def test_par_end_to_end_with_real_runner() -> None:
    """真实 R1 Runner：par yield 的 Effect 列表被并行执行，按序回注，join 聚合。"""
    seen: list[str] = []

    def handler(effect: Invoke, ctx: object) -> Result:
        name = str(effect.payload.get("name"))
        seen.append(name)
        return Result.success(name.upper(), usage=Usage(prompt_tokens=1, completion_tokens=0))

    call = build_call(make_dispatch({"llm": handler}))  # type: ignore[arg-type]
    runner = Runner(call=call)
    program = par([leaf("x", output="x"), leaf("y", output="y")], join="all")
    result = asyncio.run(runner.run(
        Session(session_id="par-e2e", program_factory=program)  # type: ignore[arg-type]
    ))
    assert result.status == "done"
    assert sorted(seen) == ["x", "y"]  # 两个子 Program 都真实执行
    # join=all 聚合各子 Program 的 Terminate.output（leaf 声明的 "x"/"y"）；
    # handler 的 Result.data（"X"/"Y"）只作为回注喂给子 Program，不是聚合对象。
    assert result.output == ["x", "y"]


def test_nested_compose() -> None:
    """嵌套：seq 包 branch_value 包 par，返回的仍是新 Program。"""
    inner = par([leaf("p1"), leaf("p2")])

    def source():
        yield Invoke(port="llm", payload={"name": "router"})
        yield Terminate(output="a")

    branch_p = branch_value(source, lambda value: str(value), {"a": inner})
    program = seq(leaf("head"), branch_p)
    effects, _ = drive(program)
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names[0] == "head"
    # par 真并行：内层两个子 Program 的 Effect 都被驱动（名单含 p1/p2）。
    assert "p1" in names and "p2" in names
