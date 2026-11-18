"""R3 超时插件（AROUND）：wait_for 包裹内层调用，超时发 degrade 信号。

ARCH-v5：apply(host) 挂 around_invoke（在 retry 内层，每次尝试独立计时）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from hooks import Decision, Host, RequestScope, Signal
from runtime import Effect, Result

__all__ = ["Timeout"]

NextFn = Callable[[], Awaitable[Decision]]


class Timeout:
    """超时插件：包裹 next_fn，超时产出 TIMEOUT 失败 + degrade 信号。"""

    name = "r3_timeout"

    def __init__(self, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout

    def apply(self, host: Host) -> None:
        host.hook("around_invoke").tap(self.name, self.call)

    async def call(
        self, next_fn: NextFn, scope: RequestScope, effect: Effect,
    ) -> Decision:
        try:
            return await asyncio.wait_for(next_fn(), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            scope.trace.append("timeout:exceeded")
            scope.emit(Signal("degrade", "timeout"))
            return Decision.fallback(
                Result.failure("TIMEOUT", f"invoke exceeded {self._timeout}s")
            )
