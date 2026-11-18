"""R3 重试插件测试（ARCH-v5）：policy.call(next_fn, scope) 包裹中心调用，失败退避重试。

领域逻辑：退避算法（指数/上限/抖动/随机源注入）、不可重试错误立即返回、重试耗尽、
幂等红线（tool 无幂等键拒重试）、Retry-After 延长退避、非 Result 载体透传。
next_fn 是 async 回调（由 AroundHook 注入的下一层），scope 是每次调用新建的 RequestScope。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import Random

from hooks import Decision, DecisionAction, RequestScope
from runtime import Effect, EffectKind

from harness.resilience import FailureClass, RetryPolicy
from harness.resilience import retry as retry_mod
from tests.r3.conftest import err_result, ok_result


def _drive(
    policy: RetryPolicy,
    results: list[object],
    *,
    scope: RequestScope | None = None,
    effect: Effect | None = None,
) -> tuple[Decision, int, RequestScope]:
    """用结果序列驱动 next_fn，返回 (裁决, 真实调用次数, scope)。

    effect 由 Hook 直送回调：幂等红线读 effect.kind / effect.idempotency_key，
    不再依赖请求容器里的手填键。
    """
    active = scope or RequestScope()
    eff = effect or Effect(EffectKind.LLM, {})
    calls = 0

    async def nxt() -> Decision:
        nonlocal calls
        result = results[min(calls, len(results) - 1)]
        calls += 1
        return Decision(action=DecisionAction.CONTINUE, value=result)

    verdict = asyncio.run(policy.call(nxt, active, eff))
    return verdict, calls, active


async def _no_sleep(_delay: float) -> None:
    """测试用 sleeper：跳过真实等待。"""


def _recording_sleeper() -> tuple[list[float], Callable[[float], Awaitable[None]]]:
    """记录每次退避时长的 sleeper，验证退避算法。"""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    return delays, sleep


class TestRetry:
    """重试语义：可重试错误退避后重试，确定性错误/耗尽/非幂等副作用拦截。"""

    def test_retry_then_success(self) -> None:
        """500 后重试成功：调 2 次，返回成功 Result。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        verdict, calls, _ = _drive(p, [err_result("500"), ok_result({"ok": 1})])
        assert calls == 2
        assert verdict.value.ok is True

    def test_non_retryable_no_retry(self) -> None:
        """400 确定性错误：只调 1 次，立即返回，不重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        verdict, calls, scope = _drive(p, [err_result("400")])
        assert calls == 1
        assert verdict.value.ok is False
        assert scope.signals == []

    def test_retry_exhausted(self) -> None:
        """一直 500：到 max_attempts 次停止，返回最后失败 Result 并发降级信号。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        verdict, calls, scope = _drive(p, [err_result("500")])
        assert calls == 3
        assert verdict.value.ok is False
        assert any(
            s.kind == "degrade" and s.reason == "retry_exhausted" for s in scope.signals
        )

    def test_backoff_exponential_no_jitter(self) -> None:
        """无抖动：退避按 base_delay × multiplier^(attempt-1) 指数增长。"""
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(
            max_attempts=3, base_delay=0.1, multiplier=2.0,
            jitter="none", sleeper=sleeper,
        )
        _drive(p, [err_result("500")])
        assert delays == [0.1, 0.2]

    def test_backoff_capped_by_max_delay(self) -> None:
        """退避不超过 max_delay（防无限增长）。"""
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(
            max_attempts=4, base_delay=0.5, multiplier=10.0, max_delay=1.0,
            jitter="none", sleeper=sleeper,
        )
        _drive(p, [err_result("500")])
        assert delays == [0.5, 1.0, 1.0]

    def test_backoff_full_jitter_uses_injected_rng(self) -> None:
        """full 抖动落在 [0, raw] 且使用注入的随机源（可回放）。"""
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(
            max_attempts=2, base_delay=1.0, multiplier=1.0,
            jitter="full", rng=Random(0), sleeper=sleeper,
        )
        _drive(p, [err_result("500")])
        assert len(delays) == 1
        assert 0.0 <= delays[0] <= 1.0
        assert delays[0] == Random(0).uniform(0.0, 1.0)

    def test_idempotency_blocks_non_idempotent_retry(self) -> None:
        """tool 副作用无幂等键：拒绝重试（deny），只调 1 次。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        effect = Effect(EffectKind.TOOL, {})  # 无 idempotency_key
        verdict, calls, _ = _drive(p, [err_result("500")], effect=effect)
        assert calls == 1
        assert verdict.action is DecisionAction.DENY
        assert "idempotency" in verdict.reason

    def test_idempotency_ok_with_key(self) -> None:
        """tool 副作用带幂等键：允许重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        effect = Effect(EffectKind.TOOL, {}, idempotency_key="k1")
        verdict, calls, _ = _drive(
            p, [err_result("500"), ok_result({"ok": 1})], effect=effect,
        )
        assert calls == 2
        assert verdict.value.ok is True

    def test_non_result_verdict_passthrough(self) -> None:
        """next 返回非 Result 载体（如 deny）：直接交上层，不解读、不重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        calls = 0

        async def nxt() -> Decision:
            nonlocal calls
            calls += 1
            return Decision.deny("blocked")

        verdict = asyncio.run(p.call(nxt, RequestScope(), Effect(EffectKind.LLM, {})))
        assert verdict.action is DecisionAction.DENY
        assert calls == 1

    def test_retry_after_extends_delay(self, monkeypatch) -> None:
        """服务端 Retry-After 提示延长退避（尊重但绝不缩短本地退避）。"""
        def classified_with_hint(result, ctx, **kwargs) -> FailureClass:
            return FailureClass(
                retryable=True, count_toward_breaker=True,
                suggested_delay=0.5, code="SERVER_ERROR",
            )

        monkeypatch.setattr(retry_mod, "classify_failure", classified_with_hint)
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(
            max_attempts=2, base_delay=0.01, multiplier=1.0,
            jitter="none", sleeper=sleeper,
        )
        _drive(p, [err_result("500"), ok_result()])
        assert delays == [0.5]
