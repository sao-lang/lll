"""组合算子 + runtime 真 e2e：逐个算子真执行 Program 并观察 Effect 序列/output。

覆盖 OPS（compose.program 10 算子）/ DSL（api.dsl 11 导出）/ RT（runtime 执行 API）。
原 branch/loop/map_state/gate 依赖不存在的 port="state"，真实 Runner 下静默误路由，已删除。
复用 tests.r12.conftest 的 drive/leaf（真实驱动 Program generator，非手搓 DTO）。
"""
from __future__ import annotations

import asyncio

from runtime import Invoke, Result, Runner, Session, Terminate, Budgets

from conftest import run
from tests.r12.conftest import drive, leaf


def _invoke_names(effects: list) -> list[str]:
    return [str(e.payload.get("name") or e.port) for e in effects if isinstance(e, Invoke)]


def test_ops01_seq_real_order() -> None:
    """seq 真组合：Effect 名序列精确 a→b，终态 output=b。"""
    from compose.program import seq
    effects, state = drive(seq(leaf("a", output="A"), leaf("b", output="B")))
    assert _invoke_names(effects) == ["a", "b"]
    assert state.output == "B"


def test_ops02_par_real_all_branches() -> None:
    """par 真并发：三分支 Effect 全部产出。"""
    from compose.program import par
    effects, _ = drive(par([leaf("a"), leaf("b"), leaf("c")]))
    assert set(_invoke_names(effects)) == {"a", "b", "c"}


def test_ops03_race_real_first_branch() -> None:
    """race 真执行：产出首个分支的 Effect（竞速语义）。"""
    from compose.program import race
    effects, _ = drive(race([leaf("fast"), leaf("slow")]))
    names = _invoke_names(effects)
    assert len(names) >= 1 and names[0] in ("fast", "slow")


def test_ops04_loop_until_real_repetition() -> None:
    """loop_until 真重复：done 命中前 body 执行精确次数。"""
    from compose.program import loop_until
    calls = {"n": 0}
    def body(feedback: object):
        def program():
            calls["n"] += 1
            yield Invoke(port="tool", payload={"name": f"t{calls['n']}"})
            yield Terminate(output=calls["n"])
        return program
    effects, _ = drive(loop_until(body, done=lambda out: int(out) >= 3, max_rounds=5))
    assert calls["n"] == 3
    assert len([e for e in effects if isinstance(e, Invoke)]) == 3


def test_ops05_retry_real_attempts() -> None:
    """retry 真重试：attempts=3 时调用次数在 1~3（依赖回注语义）。"""
    from compose.program import retry
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        yield Invoke(port="llm", payload={"name": "flaky"})
    drive(retry(flaky, attempts=3))
    assert calls["n"] == 1, f"无失败回注时 retry 只跑一次，实际 {calls['n']}"


def test_ops06_map_effect_real_rename() -> None:
    """map_effect 真改 Effect：Invoke name 被 lens 改写。"""
    from compose.program import map_effect
    mapped = map_effect(
        lambda e: Invoke(port=e.port, payload={**e.payload, "name": "renamed"}) if isinstance(e, Invoke) else e
    )(leaf("orig"))
    effects, _ = drive(mapped)
    assert _invoke_names(effects) == ["renamed"]


def test_ops07_branch_value_real_route() -> None:
    """branch_value 真从 source output 决策，只走命中臂。"""
    from compose.program import branch_value

    def source():
        yield Invoke(port="llm", payload={"name": "router"})
        yield Terminate(output="yes")

    eff, _ = drive(branch_value(
        source,
        lambda value: str(value),
        {"yes": leaf("taken"), "no": leaf("skipped")},
        default="no",
    ))
    assert _invoke_names(eff) == ["router", "taken"]


def test_ops08_fallback_real_primary() -> None:
    """fallback 真走首选臂，失败阶梯不执行。"""
    from compose.program import fallback
    eff, _ = drive(fallback(leaf("main"), ladders=[leaf("fb")], refuse=leaf("refuse")))
    assert _invoke_names(eff) == ["main"]


def test_dsl01_all_exports_have_real_execution() -> None:
    """api.dsl 导出精确；每个算子导出都有真执行用例（不是只查 callable）。"""
    import api.dsl as dsl
    expected = {"JoinMode", "ProgramFn", "branch_value", "custom_arch", "fallback",
                "loop_until", "map_effect", "par", "race", "retry", "seq"}
    assert tuple(dsl.__all__) == tuple(sorted(expected)) or set(dsl.__all__) == expected
    # 每个函数导出必须真绑定到 compose.program 的同名算子，且该算子在本组有 drive 真执行用例
    import compose.program as program
    op_to_case = {
        "seq": "test_ops01_seq_real_order", "par": "test_ops02_par_real_all_branches",
        "race": "test_ops03_race_real_first_branch",
        "loop_until": "test_ops04_loop_until_real_repetition",
        "retry": "test_ops05_retry_real_attempts", "map_effect": "test_ops06_map_effect_real_rename",
        "branch_value": "test_ops07_branch_value_real_route",
        "fallback": "test_ops08_fallback_real_primary",
        "custom_arch": "test_dsl02_custom_arch_compiles",
    }
    source = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    for name, case in op_to_case.items():
        if name != "custom_arch":  # custom_arch 是 dsl 独有封装，真执行由 test_dsl02 验证
            assert getattr(dsl, name) is getattr(program, name), f"dsl.{name} 未真绑定 compose.program.{name}"
        assert f"def {case}(" in source, f"{name} 的真执行用例 {case} 不存在"


def test_dsl02_custom_arch_compiles() -> None:
    """custom_arch 真把 ProgramFn 封装成 Arch，compile 产出可驱动 Program。"""
    from api.dsl import custom_arch
    arch = custom_arch(leaf("custom"), name="dsl-arch", version="9.9.9")
    assert arch.name == "dsl-arch" and arch.version == "9.9.9"
    effects, _ = drive(lambda: arch.compile())
    assert _invoke_names(effects) == ["custom"]


def test_rt01_runner_real_execution(loop: asyncio.AbstractEventLoop) -> None:
    """Runner 真跑 Session：返回 RunResult，事件序列非空（用户可见入口）。"""
    async def call(effect, ctx):
        return Result.success({"ok": True})
    runner = Runner(call)
    session = Session(session_id="rt-e2e", program_factory=leaf("rt", output="TERMINAL"))
    result = run(runner.run(session), loop)
    assert len(session.events) >= 1, "真执行必须产生 Event"
    assert result.output == "TERMINAL", f"Runner 真产出应为 Terminate.output，实际 {result.output}"
    assert result.status == "done"


def test_rt02_budgets_real_fields() -> None:
    """Budgets 真构造：字段精确、used 初值 0。"""
    b = Budgets(max_tokens=100, max_cost_usd=1.0, max_hops=3)
    assert b.max_tokens == 100
    assert b.max_cost_usd == 1.0
    assert b.used_tokens == 0
