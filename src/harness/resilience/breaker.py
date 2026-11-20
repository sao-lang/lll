"""R3 熔断器：三态机（closed/open/half_open）+ 滑窗失败率 + 维度隔离。

AOP 角色：check_and_signal → Warden（OPEN/半开超额 → 写 fault + 放行重路由）；
observe → Envelope 后置（从 Result 记数）。状态由插件实例 + 注入 store 持有。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol, runtime_checkable

from compose.context import FaultRecord, Invocation, Verdict
from runtime import Invoke, Result

from .types import BreakerState, BreakerStatus

__all__ = ["BreakerStore", "CircuitBreaker", "InMemoryBreakerStore"]

_EMPTY: tuple[str, str] = ("", "")
Dimension = tuple[str, str]


@runtime_checkable
class BreakerStore(Protocol):
    """熔断共享态存储协议（异步，可落 Redis/DB）。"""
    async def load(self, dim: Dimension) -> BreakerState | None: ...
    async def save(self, dim: Dimension, state: BreakerState) -> None: ...


class InMemoryBreakerStore:
    """默认熔断器存储：进程内字典，单实例闭环用。"""

    def __init__(self) -> None:
        """空内存态：按维度键存储熔断快照。"""
        self._data: dict[Dimension, BreakerState] = {}

    async def load(self, dim: Dimension) -> BreakerState | None:
        """读取维度快照；无记录返回 None。"""
        return self._data.get(dim)

    async def save(self, dim: Dimension, state: BreakerState) -> None:
        """写入维度快照（覆盖）。"""
        self._data[dim] = state


def _normalize_key(key: object) -> Dimension:
    """把外部传入的任意 key 收敛成二元组维度；形态不符回退空维度。"""
    if isinstance(key, tuple) and len(key) == 2:
        return (str(key[0]), str(key[1]))
    return _EMPTY


def _failed_target_of(inv: Invocation) -> str:
    """失败目标注册名（B2）：Invoke.payload.model 优先，缺省 "primary"。"""
    effect = inv.effect
    if isinstance(effect, Invoke):
        return str(effect.payload.get("model") or "primary")
    return "primary"


class CircuitBreaker:
    """熔断器插件：自身持有三态机状态，check 为 Warden、observe 为 Envelope 后置。

    三态机：
        CLOSED：样本足够 + 失败率≥阈值 → OPEN。
        OPEN：冷却期届满 → HALF_OPEN；否则写 fault 重路由。
        HALF_OPEN：放行有限探测；连续 N 次成功 → CLOSED，任一失败 → OPEN。
    """
    name = "r3_breaker"

    def __init__(
        self,
        *,
        failure_rate: float = 0.5,
        minimum_samples: int = 5,
        window_size: int = 20,
        open_cooldown_s: float = 30.0,
        half_open_max_probes: int = 3,
        half_open_success_to_close: int = 2,
        per_dimension: bool = True,
        clock: Callable[[], float] | None = None,
        store: BreakerStore | None = None,
        dimension_of: Callable[[Invocation], Dimension | None] | None = None,
        failed_target_of: Callable[[Invocation], str] | None = None,
    ) -> None:
        """装配熔断器（状态在本实例，跨调用存活）。

        参数：
            dimension_of: 从 Invocation 算熔断维度的纯函数（装配期注入）。
            failed_target_of: 从 Invocation 算失败目标注册名（B2）。
        """
        self._threshold = failure_rate
        self._min_samples = minimum_samples
        self._window_size = window_size
        self._cooldown = open_cooldown_s
        self._max_probes = half_open_max_probes
        self._success_to_close = half_open_success_to_close
        self._per_dimension = per_dimension
        self._clock = clock or (lambda: 0.0)
        self._store: BreakerStore = store if store is not None else InMemoryBreakerStore()
        self._dimension_of = dimension_of
        self._failed_target_of = failed_target_of or _failed_target_of
        self._locks: dict[Dimension, asyncio.Lock] = {}

    def _lock_for(self, dim: Dimension) -> asyncio.Lock:
        """取维度的 asyncio.Lock（首次懒建）。"""
        lock = self._locks.get(dim)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[dim] = lock
        return lock

    def _dim(self, dimension: Dimension | None) -> Dimension:
        """按 per_dimension 决定用调用方维度还是归并到空维度。"""
        if not self._per_dimension:
            return _EMPTY
        return dimension if dimension is not None else _EMPTY

    async def state(self, key: object) -> BreakerState:
        """读取维度当前状态（无记录视为 CLOSED 默认态）。"""
        return await self._store.load(_normalize_key(key)) or BreakerState()

    async def check(self, dimension: Dimension | None) -> str | None:
        """返回降级原因（OPEN 冷却内 / 半开超额），健康返回 None。

        整个 load→冷却迁移→reload→判定在该维度锁内完成，与 record 互斥。
        """
        dim = self._dim(dimension)
        async with self._lock_for(dim):
            st = await self._store.load(dim) or BreakerState()
            if st.status == BreakerStatus.OPEN.value:
                if self._clock() - (st.opened_at or 0) < self._cooldown:
                    return "breaker_open"
                await self._maybe_half_open_unlocked(dim)
                st = await self._store.load(dim) or BreakerState()
            if st.status == BreakerStatus.HALF_OPEN.value:
                if len(st.window) >= self._max_probes:
                    return "breaker_half_open"
            return None

    async def maybe_half_open(self, key: object) -> None:
        """公开 API：冷却期满 OPEN→HALF_OPEN（带锁）。"""
        dim = _normalize_key(key)
        async with self._lock_for(dim):
            await self._maybe_half_open_unlocked(dim)

    async def _maybe_half_open_unlocked(self, dim: Dimension) -> None:
        """无锁版本：调用方须已持有该维度锁。"""
        st = await self._store.load(dim)
        if st is None or st.status != BreakerStatus.OPEN.value:
            return
        if self._clock() - (st.opened_at or 0) >= self._cooldown:
            await self._store.save(dim, replace(
                st, status=BreakerStatus.HALF_OPEN.value, window=(),
                half_open_successes=0, opened_at=None,
            ))

    async def record(
        self, result: Result, dimension: Dimension | None = None, *, count: bool = True,
    ) -> None:
        """记录一次调用结果并推进三态机（锁内 load→decide→save）。"""
        if not count:
            return
        dim = self._dim(dimension)
        async with self._lock_for(dim):
            await self._record_unlocked(dim, result.ok)

    async def _record_unlocked(self, dim: Dimension, success: bool) -> None:
        """无锁记录：调用方须已持该维度锁。"""
        st = await self._store.load(dim) or BreakerState()
        window = (st.window + (not success,))[-self._window_size:]
        failures = st.failures + (0 if success else 1)
        if st.status == BreakerStatus.OPEN.value:
            return
        if st.status == BreakerStatus.HALF_OPEN.value:
            if success:
                successes = st.half_open_successes + 1
                if successes >= self._success_to_close:
                    await self._store.save(dim, BreakerState())
                else:
                    await self._store.save(dim, replace(st, half_open_successes=successes))
            else:
                await self._store.save(dim, replace(
                    st, status=BreakerStatus.OPEN.value, opened_at=self._clock(),
                    half_open_successes=0,
                ))
            return
        new_state = replace(st, window=window, failures=failures)
        if len(window) >= self._min_samples:
            rate = sum(1 for bad in window if bad) / len(window)
            if rate >= self._threshold:
                new_state = replace(
                    new_state, status=BreakerStatus.OPEN.value, opened_at=self._clock()
                )
        await self._store.save(dim, new_state)

    # ---- AOP 角色方法 ----
    def _dimension(self, inv: Invocation) -> Dimension | None:
        """从 Invocation 派生熔断维度（自定义函数非二元组时回退空维度）。"""
        if self._dimension_of is None:
            return None
        dim = self._dimension_of(inv)
        return dim if isinstance(dim, tuple) else None

    async def guard(self, inv: Invocation) -> Verdict:
        """Warden.guard：检查熔断状态，异常写 fault（含注册名，B2）并放行重路由。"""
        reason = await self.check(self._dimension(inv))
        if reason:
            inv.write_fault(FaultRecord(
                reason=reason, failed_target=self._failed_target_of(inv),
            ))
            return Verdict(allowed=True)  # 降级类放行，容器据此重路由
        return Verdict(allowed=True)

    async def observe(self, inv: Invocation, result: Result) -> None:
        """Envelope 后置：从 Result 记数（供容器在中心调用后调用）。"""
        await self.record(result, self._dimension(inv))

    async def wrap(self, inv: Invocation, next: Callable[[Invocation], Awaitable[Result]]) -> Result:
        """Envelope.wrap：前置无动作，中心调用后记数（异常按失败记、不吞异常）。"""
        try:
            result = await next(inv)
        except Exception:
            # 异常按一次失败记入熔断滑窗，随后原样抛回容器交 Salvage 处理。
            await self.record(Result.failure("INVOKE_EXCEPTION", "invoke raised"), self._dimension(inv))
            raise
        await self.record(result, self._dimension(inv))
        return result
