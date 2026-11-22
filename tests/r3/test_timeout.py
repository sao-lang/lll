"""R3 超时插件测试（领域语义）：run_with_deadline(inv, next) 超时写故障记录 + TIMEOUT 失败。

领域逻辑：超时→TIMEOUT 失败 Result + fault（含 failed_target，R3-7）、未超时透传、
超时时长可配、TIMEOUT 失败被分类为可重试。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from compose.context import Invocation
from runtime import Invoke, Result

from harness.resilience import Timeout, classify_failure
from tests.r3.conftest import make_ctx, ok_result

type NextFn = Callable[[Invocation], Awaitable[Result]]


async def _never(i: Invocation) -> Result:
    """永不完成的下一层回调（模拟卡死的 Port 调用）。"""
    await asyncio.Event().wait()
    return Result.success("unreachable")


def _done(value: object = "ok") -> NextFn:
    """立即完成的下一层回调工厂。"""
    async def nxt(i: Invocation) -> Result:
        return Result.success(value)
    return nxt


async def _slow(i: Invocation) -> Result:
    """耗时 20ms 的下一层回调，用于验证超时时长可配。"""
    await asyncio.sleep(0.02)
    return ok_result()


def _call(
    timeout: Timeout,
    next_fn: NextFn,
    effect: Invoke | None = None,
) -> tuple[Result, Invocation]:
    inv = make_ctx(effect)
    return asyncio.run(timeout.run_with_deadline(inv, next_fn)), inv


class TestTimeout:
    def test_timeout_returns_failure_and_fault(self) -> None:
        """超时 → TIMEOUT 失败 Result + fault（reason=timeout）。"""
        t = Timeout(timeout=0.01)
        result, inv = _call(t, _never)
        assert not result.ok and result.error is not None and result.error.code == "TIMEOUT"
        fault = inv.fault()
        assert fault is not None and fault.reason == "timeout"

    def test_timeout_fault_carries_failed_target(self) -> None:
        """R3-7：超时故障记录带 failed_target（注册名）。"""
        eff = Invoke(port="llm", payload={"model": "primary"})
        _, inv = _call(Timeout(timeout=0.01), _never, effect=eff)
        fault = inv.fault()
        assert fault is not None and fault.failed_target == "primary", \
            f"timeout 须带失败目标注册名，实际 {fault.failed_target if fault else None!r}"

    def test_no_timeout_passes_through(self) -> None:
        """未超时 → 原样透传内层 Result，无 fault。"""
        nxt = _done()
        result, inv = _call(Timeout(timeout=1.0), nxt)
        assert result.ok and result.data == "ok"
        assert inv.fault() is None

    def test_timeout_configurable(self) -> None:
        """超时时长可配：短超时失败、长超时放行。"""
        result_short, _ = _call(Timeout(timeout=0.001), _slow)
        assert not result_short.ok and result_short.error is not None
        assert result_short.error.code == "TIMEOUT"
        result_long, _ = _call(Timeout(timeout=0.2), _slow)
        assert result_long.ok

    def test_timeout_result_is_retryable(self) -> None:
        """TIMEOUT 失败 Result 被失败分类识别为可重试（触发 RetryPolicy 联动）。"""
        t = Timeout(timeout=0.01)
        result, _ = _call(t, _never)
        fc = classify_failure(result, make_ctx())
        assert fc.retryable is True
