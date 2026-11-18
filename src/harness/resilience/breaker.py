"""R3 熔断器：三态机（closed/open/half_open）+ 滑窗失败率 + 维度隔离。

ARCH-v5：状态（store + 三态机）直接由插件实例持有——插件注册一次、长期存活，
天然跨调用；无独立 Policy 层。OPEN/半开超额 → scope.emit(degrade 信号) + 占位 DEGRADE。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, runtime_checkable

from hooks import Decision, Host, RequestScope, Signal
from runtime import Effect, Result

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
        self._data: dict[Dimension, BreakerState] = {}

    async def load(self, dim: Dimension) -> BreakerState | None:
        return self._data.get(dim)

    async def save(self, dim: Dimension, state: BreakerState) -> None:
        self._data[dim] = state


def _normalize_key(key: object) -> Dimension:
    if isinstance(key, tuple) and len(key) == 2:
        return (str(key[0]), str(key[1]))
    return _EMPTY


def _default_failed_target(effect: Effect) -> str | None:
    """默认失败目标：后端注册名（payload.model）优先，缺省 "primary"。

    B2 修复点：degrade 信号的 failed_target 必须是降级阶梯里的注册名，
    不能是维度字符串（维度是 ("provider", name) 二元组，网关无法据此切后端）。
    """
    # 规则 6 例外：model 是 EffectPayload 的具名键，此处在边界外读取注册名。
    return effect.payload.get("model") or "primary"


class CircuitBreaker:
    """熔断器插件：自身持有三态机状态，apply(host) 注册 BAIL 守卫与 PARALLEL 观察。

    三态机：
        CLOSED：样本足够 + 失败率≥阈值 → OPEN。
        OPEN：冷却期届满 → HALF_OPEN；否则 DEGRADE 拦截。
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
        dimension_of: Callable[[Effect], Dimension | None] | None = None,
        failed_target_of: Callable[[Effect], str | None] | None = None,
    ) -> None:
        """装配熔断器（状态直接在本实例，跨调用存活）。

        dimension_of: 从 effect 算熔断维度的纯函数（装配期注入）。
        failed_target_of: 从 effect 算失败目标注册名的纯函数（B2：degrade 信号用）。
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
        self._failed_target_of = failed_target_of or _default_failed_target

    def apply(self, host: Host) -> None:
        host.hook("before_invoke").tap(self.name, self.check_and_signal)
        host.hook("after_invoke").tap(self.name, self.observe)
        host.hook("invoke_error").tap(self.name, self.observe_error)

    # ---- 状态逻辑（原 Policy 方法，现直接在插件上）----

    def _dim(self, dimension: Dimension | None) -> Dimension:
        if not self._per_dimension:
            return _EMPTY
        return dimension if dimension is not None else _EMPTY

    async def state(self, key: object) -> BreakerState:
        return await self._store.load(_normalize_key(key)) or BreakerState()

    async def check(self, dimension: Dimension | None) -> str | None:
        """返回降级原因（OPEN 冷却内 / 半开超额），健康返回 None。"""
        dim = self._dim(dimension)
        st = await self.state(dim)
        if st.status == BreakerStatus.OPEN.value:
            if self._clock() - (st.opened_at or 0) < self._cooldown:
                return "breaker_open"
            await self.maybe_half_open(dim)
            st = await self.state(dim)
        if st.status == BreakerStatus.HALF_OPEN.value:
            if len(st.window) >= self._max_probes:
                return "breaker_half_open"
        return None

    async def maybe_half_open(self, key: object) -> None:
        dim = _normalize_key(key)
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
        dim = self._dim(dimension)
        st = await self._store.load(dim) or BreakerState()
        if not count:
            return
        success = result.ok
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

    # ---- Hook tap 回调 ----

    async def check_and_signal(
        self, scope: RequestScope, effect: Effect,
    ) -> Decision | None:
        """before_invoke BAIL：检查熔断状态，异常则发降级信号 + 占位 DEGRADE。"""
        dim = self._dimension_of(effect) if self._dimension_of else None
        reason = await self.check(dim if isinstance(dim, tuple) else None)
        if reason:
            failed_target = self._failed_target_of(effect)
            scope.emit(Signal("degrade", reason, failed_target))
            return Decision.degrade("")
        return None

    async def observe(self, scope: RequestScope, effect: Effect) -> None:
        """after_invoke / invoke_error PARALLEL：从 scope.last_result 记录结果。"""
        result = scope.last_result
        if isinstance(result, Result):
            dim = self._dimension_of(effect) if self._dimension_of else None
            await self.record(result, dim if isinstance(dim, tuple) else None)

    async def observe_error(self, scope: RequestScope, effect: Effect) -> None:
        # invoke_error 由 host.call 写入 last_result（修复旧断点）
        await self.observe(scope, effect)
