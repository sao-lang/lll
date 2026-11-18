"""R3 重试插件（AROUND）：包裹 next 调用，失败按退避算法重试。

ARCH-v5：apply(host) 把 retry 挂到 around_invoke（AROUND）；
耗尽时向 scope.emit(degrade 信号)，由 settle 阶段统一裁决降级。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import Random

from hooks import Decision, Host, RequestScope, Signal
from runtime import Effect, EffectKind, Result

from .types import Jitter, backoff_delay, classify_failure, validate_retry_spec

__all__ = ["RetryPolicy"]

NextFn = Callable[[], Awaitable[Decision]]


class RetryPolicy:
    """重试插件：AROUND 包裹 Port 调用，幂等红线保护。"""

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
    ) -> None:
        from hooks import RetrySpec

        self._spec = RetrySpec(
            max_attempts=max_attempts, base_delay=base_delay, multiplier=multiplier,
            max_delay=max_delay, jitter=Jitter.coerce(jitter).value,
            respect_retry_after=respect_retry_after,
            idempotency_required=idempotency_required,
        )
        validate_retry_spec(self._spec)
        self._rng = rng if rng is not None else Random()
        self._sleep = sleeper

    def apply(self, host: Host) -> None:
        host.hook("around_invoke").tap(self.name, self.call)

    async def call(
        self, next_fn: NextFn, scope: RequestScope, effect: Effect,
    ) -> Decision:
        """AROUND 入口：循环调 next_fn，失败退避重试。

        E1 修复点：生产 core 返回裸 Result（无 .value），测试用 Decision(value=Result)，
        两种载体都兼容：Decision 取 .value，其余按裸 Result 处理。
        """
        sleeper = self._sleep or asyncio.sleep
        prev_delay = 0.0
        last = Decision.continue_()
        for attempt in range(1, self._spec.max_attempts + 1):
            verdict = await next_fn()
            last = verdict
            result = verdict.value if isinstance(verdict, Decision) else verdict
            if not isinstance(result, Result):
                return verdict
            fc = classify_failure(result, scope)
            if not fc.retryable or result.ok:
                return verdict
            scope.trace.append(f"retry:classified:{fc.code}")
            if attempt >= self._spec.max_attempts:
                scope.trace.append("retry:exhausted")
                scope.emit(Signal("degrade", "retry_exhausted"))
                return verdict
            if self._is_retryable_side_effect(scope, effect):
                return Decision.deny("idempotency_required for side-effect retry")
            delay = backoff_delay(
                attempt, self._spec, self._rng,
                prev_delay=prev_delay, retry_after=fc.suggested_delay,
            )
            prev_delay = delay
            await sleeper(delay)
        return last

    def _is_retryable_side_effect(self, scope: RequestScope, effect: Effect) -> bool:
        """幂等红线：TOOL 副作用无幂等键时禁止重试。"""
        if not self._spec.idempotency_required:
            return False
        if effect.kind != EffectKind.TOOL:
            return False
        return not bool(effect.idempotency_key)
