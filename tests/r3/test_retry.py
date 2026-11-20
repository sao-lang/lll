"""R3 重试插件测试（AOP Envelope）：wrap(inv, next) 包裹中心调用，失败退避重试。

领域逻辑：退避算法（指数/上限/抖动/注入 RNG）、不可重试错误立即返回、重试耗尽写 fault、
幂等红线、Retry-After 延长退避。R3-7：耗尽故障记录带 failed_target。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import Random

from aop import Invocation
from runtime import Invoke, Result

from harness.resilience import FailureClass, RetryPolicy
from harness.resilience import retry as retry_mod
from tests.r3.conftest import err_result, make_ctx, ok_result

type NextFn = Callable[[Invocation], Awaitable[Result]]


def _drive(
    policy: RetryPolicy,
    results: list[Result],
    *,
    effect: Invoke | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[Result, int, Invocation]:
    """用结果序列驱动 next，返回 (Result, 真实调用次数, inv)。"""
    inv = make_ctx(effect)
    calls = 0

    async def nxt(i: Invocation) -> Result:
        nonlocal calls
        result = results[min(calls, len(results) - 1)]
        calls += 1
        return result

    if sleeper is not None:
        policy._sleep = sleeper
    result = asyncio.run(policy.wrap(inv, nxt))
    return result, calls, inv


async def _no_sleep(_delay: float) -> None:
    """测试用 sleeper：跳过真实等待。"""


def _recording_sleeper() -> tuple[list[float], Callable[[float], Awaitable[None]]]:
    """记录每次退避时长的 sleeper。"""
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    return delays, sleep


class TestRetry:
    def test_retry_then_success(self) -> None:
        """500 后重试成功：调 2 次，返回成功 Result。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        result, calls, _ = _drive(p, [err_result("500"), ok_result({"ok": 1})])
        assert calls == 2 and result.ok

    def test_non_retryable_no_retry(self) -> None:
        """400 确定性错误：只调 1 次，立即返回，不写 fault。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        result, calls, inv = _drive(p, [err_result("400")])
        assert calls == 1 and not result.ok
        assert inv.fault() is None

    def test_retry_exhausted_writes_fault(self) -> None:
        """一直 500：到 max_attempts 次停止，返回最后失败 Result 并写 fault。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        result, calls, inv = _drive(p, [err_result("500")])
        assert calls == 3 and not result.ok
        fault = inv.fault()
        assert fault is not None and fault.reason == "retry_exhausted"

    def test_retry_exhausted_fault_carries_failed_target(self) -> None:
        """R3-7：耗尽故障记录带 failed_target（注册名），settle 才能剔除。"""
        p = RetryPolicy(max_attempts=2, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        eff = Invoke(port="llm", payload={"model": "primary"})
        _, _, inv = _drive(p, [err_result("500")], effect=eff)
        fault = inv.fault()
        assert fault is not None and fault.failed_target == "primary", \
            f"retry_exhausted 须带失败目标注册名，实际 {fault.failed_target if fault else None!r}"

    def test_backoff_exponential_no_jitter(self) -> None:
        """无抖动：退避按 base_delay × multiplier^(attempt-1) 指数增长。"""
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(max_attempts=3, base_delay=0.1, multiplier=2.0, jitter="none")
        _drive(p, [err_result("500")], sleeper=sleeper)
        assert delays == [0.1, 0.2]

    def test_backoff_capped_by_max_delay(self) -> None:
        """退避不超过 max_delay。"""
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(max_attempts=4, base_delay=0.5, multiplier=10.0, max_delay=1.0, jitter="none")
        _drive(p, [err_result("500")], sleeper=sleeper)
        assert delays == [0.5, 1.0, 1.0]

    def test_backoff_full_jitter_uses_injected_rng(self) -> None:
        """full 抖动落在 [0, raw] 且用注入 RNG（可回放）。"""
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(max_attempts=2, base_delay=1.0, multiplier=1.0, jitter="full", rng=Random(0))
        _drive(p, [err_result("500")], sleeper=sleeper)
        assert len(delays) == 1 and 0.0 <= delays[0] <= 1.0
        assert delays[0] == Random(0).uniform(0.0, 1.0)

    def test_idempotency_blocks_non_idempotent_retry(self) -> None:
        """tool 副作用无幂等键：拒绝重试，只调 1 次，返回失败。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        effect = Invoke(port="tool", payload={})
        result, calls, _ = _drive(p, [err_result("500")], effect=effect)
        assert calls == 1 and not result.ok
        assert result.error is not None and result.error.code == "IDEMPOTENCY_REQUIRED"

    def test_idempotency_ok_with_key(self) -> None:
        """tool 副作用带幂等键：允许重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        effect = Invoke(port="tool", payload={}, idempotency_key="k1")
        result, calls, _ = _drive(p, [err_result("500"), ok_result({"ok": 1})], effect=effect)
        assert calls == 2 and result.ok

    def test_retry_after_extends_delay(self, monkeypatch) -> None:
        """服务端 Retry-After 延长退避。"""
        def classified_with_hint(result, ctx, **kwargs) -> FailureClass:
            return FailureClass(retryable=True, count_toward_breaker=True,
                                suggested_delay=0.5, code="SERVER_ERROR")
        monkeypatch.setattr(retry_mod, "classify_failure", classified_with_hint)
        delays, sleeper = _recording_sleeper()
        p = RetryPolicy(max_attempts=2, base_delay=0.01, multiplier=1.0, jitter="none")
        _drive(p, [err_result("500"), ok_result()], sleeper=sleeper)
        assert delays == [0.5]
