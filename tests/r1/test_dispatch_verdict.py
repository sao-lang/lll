"""R1 _execute 裁决分支：用鸭子类型 outcome 测（内核不依赖 R2 的 Decision）。

覆盖 dispatch 返回的各类裁决：None→call / Result→直接用 / abort|deny→失败 /
fallback→用 value / value 是 Result→直接用。防止内核裁决语义回退。
"""
import asyncio
from dataclasses import dataclass

from runtime import AgentState, Effect, EffectKind, Result, Runner, Session

CALL_LOG: list[str] = []


async def fake_call(effect: Effect, ctx) -> Result:
    CALL_LOG.append("called")
    return Result.success({"content": "FROM_CALL"})


async def fake_dispatch(effect: Effect, ctx):
    return None


@dataclass
class Verdict:
    """鸭子类型判决对象（模拟 R2 Decision 的字段形态）。"""
    action: str = "continue"
    reason: str = ""
    value: object = None
    data: object = None


async def run_with(outcome) -> Result:
    """跑一步拿到 Result。"""
    CALL_LOG.clear()

    async def dispatch(effect, ctx):
        return outcome

    runner = Runner(dispatch=dispatch, call=fake_call)
    session = Session(session_id="v", program_factory=lambda: (yield Effect(EffectKind.LLM, {})))
    # 用 step 直接拿 result，避免 program 后续处理
    report = await runner.step(session)
    return report.results[0] if report.results else Result.failure("NO_RESULT", "")


def test_none_dispatches_to_call():
    """dispatch 返回 None → 调 Port。"""
    r = asyncio.run(run_with(None))
    assert r.ok and CALL_LOG == ["called"]


def test_result_directly_returned():
    """dispatch 返回 Result → 直接用，不调 Port。"""
    r = asyncio.run(run_with(Result.success("DIRECT")))
    assert r.ok and r.data == "DIRECT" and not CALL_LOG


def test_abort_verdict():
    """abort 裁决 → 失败，不调 Port。"""
    r = asyncio.run(run_with(Verdict(action="abort", reason="stop")))
    assert not r.ok and r.error and r.error.code == "CHAIN_ABORTED" and not CALL_LOG


def test_deny_verdict():
    """deny 裁决 → 失败，不调 Port。"""
    r = asyncio.run(run_with(Verdict(action="deny", reason="blocked")))
    assert not r.ok and r.error and r.error.code == "CHAIN_ABORTED" and not CALL_LOG


def test_fallback_value():
    """fallback → 用 value 作结果，不调 Port。"""
    r = asyncio.run(run_with(Verdict(action="fallback", value="FALLBACK")))
    assert r.ok and r.data == "FALLBACK" and not CALL_LOG


def test_value_is_result():
    """value 装 Result → 直接用（around 中心 invoke 的路径）。"""
    inner = Result.success("INNER")
    r = asyncio.run(run_with(Verdict(action="continue", value=inner)))
    assert r is inner and not CALL_LOG


def test_continue_falls_to_call():
    """continue 且无 value → 调 Port（正常路径）。"""
    r = asyncio.run(run_with(Verdict(action="continue")))
    assert r.ok and CALL_LOG == ["called"]


def test_abort_callable_not_treated_as_flag():
    """回归：abort 是 callable（方法）时不得当真值误判。"""
    @dataclass
    class WithMethod:
        action: str = "continue"
        def abort(self):  # 模拟 Decision.abort 类方法
            return "should not abort"
    r = asyncio.run(run_with(WithMethod()))
    assert r.ok and CALL_LOG == ["called"]
