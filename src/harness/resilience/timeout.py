"""R3 超时插件（AROUND）：wait_for 包裹内层调用，超时发 degrade 信号。

ARCH-v5：apply(host) 挂 around_invoke（在 retry 内层，每次尝试独立计时）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from hooks import Decision, Host, RequestScope, Signal, SignalKind
from runtime import Effect, Result

__all__ = ["Timeout"]

NextFn = Callable[[], Awaitable[Decision]]


def _default_failed_target(effect: Effect) -> str | None:
    """默认失败目标：后端注册名（payload.model）优先，缺省 "primary"（与 breaker/retry 同口径）。

    规则 6 例外：model 是 EffectPayload 的具名键，此处在边界外读取注册名。
    """
    return effect.payload.get("model") or "primary"


class Timeout:
    """超时插件：包裹 next_fn，超时产出 TIMEOUT 失败 + degrade 信号。"""

    name = "r3_timeout"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        failed_target_of: Callable[[Effect], str | None] | None = None,
    ) -> None:
        """构造超时插件。

        参数：
            failed_target_of: 从 Effect 派生失败目标注册名的纯函数（R3-7：超时信号带
                failed_target）；缺省取 payload.model 或 "primary"。
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._failed_target_of = failed_target_of or _default_failed_target

    def apply(self, host: Host) -> None:
        """挂载 around_invoke（AROUND，内层）：每次尝试独立计时。"""
        host.hook("around_invoke").tap(self.name, self.call)

    async def call(
        self, next_fn: NextFn, scope: RequestScope, effect: Effect,
    ) -> Decision:
        """AROUND 回调：超时则发降级信号 + TIMEOUT 兜底，否则透传内层结果。"""
        try:
            return await asyncio.wait_for(next_fn(), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            scope.trace.append("timeout:exceeded")
            # R3-7：带上失败目标注册名，settle 才能剔除失败后端。
            scope.emit(Signal(SignalKind.DEGRADE, "timeout", self._failed_target_of(effect)))
            return Decision.fallback(
                Result.failure("TIMEOUT", f"invoke exceeded {self._timeout}s")
            )
