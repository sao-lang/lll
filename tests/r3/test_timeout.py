"""R3 超时插件测试（ARCH-v5）：t.call(next_fn, scope) 包裹内层调用，超时发降级信号。

领域逻辑：超时→fallback（装 TIMEOUT 失败 Result）+ degrade 信号、未超时透传、
超时时长可配、无 next 载体（中心调用未产出 Result）原样放行。
next_fn 是 async 回调（由 AroundHook 注入的下一层），scope 是每次调用新建的 RequestScope。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from hooks import Decision, DecisionAction, RequestScope

from harness.resilience import Timeout, classify_failure
from runtime import Effect, EffectKind, Result
from tests.r3.conftest import ok_result


async def _never() -> Decision:
    """永不完成的下一层回调（模拟卡死的 Port 调用）。"""
    await asyncio.Event().wait()
    return Decision.continue_()


async def _done(value: object = "ok") -> Decision:
    """立即完成的下一层回调，Result 装在 value 里。"""
    return Decision(action=DecisionAction.CONTINUE, value=Result.success(value))


async def _slow() -> Decision:
    """耗时 20ms 的下一层回调，用于验证超时时长可配。"""
    await asyncio.sleep(0.02)
    return Decision(action=DecisionAction.CONTINUE, value=ok_result())


async def _bare() -> Decision:
    """中心调用未产出 Result 载体（v5 下"无 next 结果"的等价场景）。"""
    return Decision.continue_()


def _call(
    timeout: Timeout,
    next_fn: Callable[[], Awaitable[Decision]],
    scope: RequestScope | None = None,
    effect: Effect | None = None,
) -> tuple[Decision, RequestScope]:
    active = scope or RequestScope()
    eff = effect or Effect(EffectKind.LLM, {})
    verdict = asyncio.run(timeout.call(next_fn, active, eff))
    return verdict, active


class TestTimeout:
    def test_timeout_returns_fallback_with_timeout_result(self) -> None:
        """超时 → fallback 装 TIMEOUT 失败 Result，并发 degrade 信号、写 trace。"""
        t = Timeout(timeout=0.01)
        d, scope = _call(t, _never)
        assert d.action is DecisionAction.FALLBACK
        assert isinstance(d.value, Result)
        assert d.value.ok is False
        assert d.value.error is not None and d.value.error.code == "TIMEOUT"
        assert any(s.kind == "degrade" and s.reason == "timeout" for s in scope.signals)
        assert any("timeout" in x for x in scope.trace)

    def test_timeout_signal_carries_failed_target(self) -> None:
        """R3-7：超时降级信号带 failed_target，settle 才能剔除失败后端。"""
        eff = Effect(EffectKind.LLM, {"model": "primary"})
        _, scope = _call(Timeout(timeout=0.01), _never, effect=eff)
        sig = next(s for s in scope.signals if s.reason == "timeout")
        assert sig.failed_target == "primary", \
            f"timeout 信号须带失败目标注册名，实际 {sig.failed_target!r}"

    def test_no_timeout_passes_through(self) -> None:
        """未超时 → 原样透传内层 Decision。"""
        t = Timeout(timeout=1.0)
        d, scope = _call(t, _done)
        assert d.action is DecisionAction.CONTINUE
        assert isinstance(d.value, Result) and d.value.ok is True
        assert scope.signals == []

    def test_timeout_configurable(self) -> None:
        """超时时长可配：同一内层调用，短超时触发兜底、长超时放行。"""
        d_short, _ = _call(Timeout(timeout=0.001), _slow)
        assert d_short.action is DecisionAction.FALLBACK
        assert d_short.value.error is not None and d_short.value.error.code == "TIMEOUT"

        d_long, _ = _call(Timeout(timeout=0.2), _slow)
        assert d_long.action is DecisionAction.CONTINUE
        assert isinstance(d_long.value, Result) and d_long.value.ok is True

    def test_no_next_passes_through(self) -> None:
        """无 next 结果（中心调用返回裸 Decision）→ 放行，不解读、不报错。"""
        t = Timeout(timeout=0.01)
        d, scope = _call(t, _bare)
        assert d.action is DecisionAction.CONTINUE
        assert d.value is None
        assert scope.signals == []

    def test_timeout_result_is_retryable(self) -> None:
        """TIMEOUT 失败 Result 被失败分类识别为可重试（触发 RetryPolicy 联动）。"""
        t = Timeout(timeout=0.01)
        d, _ = _call(t, _never)
        fc = classify_failure(d.value, RequestScope())  # type: ignore[arg-type]
        assert fc.retryable is True
