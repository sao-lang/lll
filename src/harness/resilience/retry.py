"""R3 重试插件（Envelope）：包裹 next 调用，失败按退避算法重试。

耗尽时把具名故障记录写入 inv.meta（含 failed_target，R3-7），交 Degradation 选级。
幂等红线：无幂等键的 TOOL 副作用禁止重试。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import Random

from aop import FaultRecord, Invocation
from runtime import Invoke, PortName, Result

from .types import Jitter, RetrySpec, backoff_delay, classify_failure, validate_retry_spec

__all__ = ["RetryPolicy"]

type NextFn = Callable[[Invocation], Awaitable[Result]]


def _failed_target_of(inv: Invocation) -> str:
    """失败目标注册名（R3-7）：Invoke.payload.model 优先，缺省 "primary"。"""
    effect = inv.effect
    if isinstance(effect, Invoke):
        return str(effect.payload.get("model") or "primary")
    return "primary"


class RetryPolicy:
    """重试插件（Envelope）：包裹 Port 调用，幂等红线保护。"""
    name = "r3_retry"

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        multiplier: float = 2.0,
        max_delay: float = 30.0,
        jitter: Jitter | str = Jitter.FULL,
        respect_retry_after: bool = True,
        idempotency_required: bool = True,
        rng: Random | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        failed_target_of: Callable[[Invocation], str] | None = None,
    ) -> None:
        """构造重试策略。

        参数：
            failed_target_of: 从 Invocation 派生失败目标注册名（R3-7）。
        """
        self._failed_target_of = failed_target_of or _failed_target_of
        self._spec = RetrySpec(
            max_attempts=max_attempts, base_delay=base_delay, multiplier=multiplier,
            max_delay=max_delay, jitter=Jitter.coerce(jitter).value,
            respect_retry_after=respect_retry_after,
            idempotency_required=idempotency_required,
        )
        validate_retry_spec(self._spec)
        self._rng = rng if rng is not None else Random()
        self._sleep = sleeper

    async def wrap(self, inv: Invocation, next: NextFn) -> Result:
        """Envelope.wrap：循环调 next，失败退避重试；耗尽写 fault。"""
        sleeper = self._sleep or asyncio.sleep
        prev_delay = 0.0
        last = Result.failure("RETRY_EXHAUSTED", "no attempts")
        for attempt in range(1, self._spec.max_attempts + 1):
            result = await next(inv)
            last = result
            fc = classify_failure(result, inv)
            if not fc.retryable or result.ok:
                return result
            inv.meta["retry_classified"] = fc.code
            if attempt >= self._spec.max_attempts:
                # 耗尽：写具名故障记录（含 failed_target，R3-7）。
                inv.write_fault(FaultRecord(
                    reason="retry_exhausted", failed_target=self._failed_target_of(inv),
                ))
                return result
            if self._is_retryable_side_effect(inv):
                return Result.failure(
                    "IDEMPOTENCY_REQUIRED", "idempotency_required for side-effect retry"
                )
            delay = backoff_delay(
                attempt, self._spec, self._rng,
                prev_delay=prev_delay, retry_after=fc.suggested_delay,
            )
            prev_delay = delay
            await sleeper(delay)
        return last

    def _is_retryable_side_effect(self, inv: Invocation) -> bool:
        """幂等红线：TOOL 副作用无幂等键时禁止重试。"""
        if not self._spec.idempotency_required:
            return False
        effect = inv.effect
        if not isinstance(effect, Invoke) or effect.port != PortName.TOOL.value:
            return False
        return not bool(effect.idempotency_key)
