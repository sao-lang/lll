"""R3 超时插件：时限执行（领域语义），超时写故障记录（含 failed_target，R3-7）。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from compose.context import FaultRecord, Invocation

from .defenses import failed_target_of
from runtime import Invoke, Result

__all__ = ["Timeout"]

type NextFn = Callable[[Invocation], Awaitable[Result]]


class Timeout:
    """时限策略：在给定时限内执行下一跳，超时写故障记录 + 超时失败 Result。"""
    name = "r3_timeout"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        target_of: Callable[[Invocation], str] | None = None,
    ) -> None:
        """构造时限策略。

        参数：
            target_of: 从 Invocation 派生失败目标注册名的纯函数（R3-7），
                缺省用领域默认 failed_target_of。
        异常：
            ValueError: timeout <= 0。
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._target_of = target_of or failed_target_of

    async def run_with_deadline(self, inv: Invocation, next: NextFn) -> Result:
        """时限执行（领域语义）：超时则写 fault + TIMEOUT 失败，否则透传。"""
        try:
            return await asyncio.wait_for(next(inv), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            inv.write_fault(FaultRecord(
                reason="timeout", failed_target=self._target_of(inv),
            ))
            return Result.failure("TIMEOUT", f"invoke exceeded {self._timeout}s")
