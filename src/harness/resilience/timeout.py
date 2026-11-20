"""R3 超时插件（Envelope）：wait_for 包裹 next，超时写故障记录（含 failed_target，R3-7）。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from compose.context import FaultRecord, Invocation
from runtime import Invoke, Result

__all__ = ["Timeout"]

type NextFn = Callable[[Invocation], Awaitable[Result]]


def _failed_target_of(inv: Invocation) -> str:
    """失败目标注册名（R3-7）：Invoke.payload.model 优先，缺省 "primary"。"""
    effect = inv.effect
    if isinstance(effect, Invoke):
        return str(effect.payload.get("model") or "primary")
    return "primary"


class Timeout:
    """超时插件（Envelope）：包裹 next，超时写故障记录 + 超时失败 Result。"""
    name = "r3_timeout"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        failed_target_of: Callable[[Invocation], str] | None = None,
    ) -> None:
        """构造超时插件。

        参数：
            failed_target_of: 从 Invocation 派生失败目标注册名（R3-7）。
        异常：
            ValueError: timeout <= 0。
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._failed_target_of = failed_target_of or _failed_target_of

    async def wrap(self, inv: Invocation, next: NextFn) -> Result:
        """Envelope.wrap：超时则写 fault + TIMEOUT 失败，否则透传。"""
        try:
            return await asyncio.wait_for(next(inv), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            inv.write_fault(FaultRecord(
                reason="timeout", failed_target=self._failed_target_of(inv),
            ))
            return Result.failure("TIMEOUT", f"invoke exceeded {self._timeout}s")
