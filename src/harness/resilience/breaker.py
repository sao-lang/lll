"""R3 熔断器：三态机（closed/open/half_open）+ 滑窗失败率 + 维度隔离。

ARCH-v5：状态（store + 三态机）直接由插件实例持有——插件注册一次、长期存活，
天然跨调用；无独立 Policy 层。OPEN/半开超额 → scope.emit(degrade 信号) + 占位 DEGRADE。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, runtime_checkable

from hooks import Decision, Host, RequestScope, Signal, SignalKind
from runtime import Effect, Result

from .types import BreakerState, BreakerStatus

__all__ = ["BreakerStore", "CircuitBreaker", "InMemoryBreakerStore"]

_EMPTY: tuple[str, str] = ("", "")
Dimension = tuple[str, str]


@runtime_checkable
class BreakerStore(Protocol):
    """熔断共享态存储协议（异步，可落 Redis/DB）。"""

    async def load(self, dim: Dimension) -> BreakerState | None:
        """读取维度熔断快照；无记录返回 None，由调用方回退 CLOSED 默认态。"""
        ...

    async def save(self, dim: Dimension, state: BreakerState) -> None:
        """覆盖写入维度熔断快照（整份替换，非增量合并）。"""
        ...


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
    """把外部传入的任意 key 收敛成二元组维度；形态不符一律回退空维度，避免污染 store key。"""
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
        # C1：按维度加锁，保护 store 的 load→decide→save 读改写临界区，
        # 避免异步 store（Redis/DB）下并发交错丢更新。进程内 shed 锁；多进程部署需 store 端原子操作。
        self._locks: dict[Dimension, asyncio.Lock] = {}

    def _lock_for(self, dim: Dimension) -> asyncio.Lock:
        """取维度的 asyncio.Lock（首次懒建；单线程事件循环下创建原子）。"""
        lock = self._locks.get(dim)
        if lock is None:  # 懒建：只给真正出现过的维度建锁，避免预建所有可能 key 导致字典无界增长
            lock = asyncio.Lock()
            self._locks[dim] = lock
        return lock

    def apply(self, host: Host) -> None:
        """挂载三个扩展点：守卫检查、成功/失败结果回喂计数。"""
        host.hook("before_invoke").tap(self.name, self.check_and_signal)
        host.hook("after_invoke").tap(self.name, self.observe)
        host.hook("invoke_error").tap(self.name, self.observe_error)

    # ---- 状态逻辑（原 Policy 方法，现直接在插件上）----

    def _dim(self, dimension: Dimension | None) -> Dimension:
        """按 per_dimension 决定用调用方维度，还是归并到单一空维度（全局熔断）。"""
        if not self._per_dimension:
            return _EMPTY
        return dimension if dimension is not None else _EMPTY

    async def state(self, key: object) -> BreakerState:
        """读取维度当前状态（无记录视为 CLOSED 默认态）。"""
        return await self._store.load(_normalize_key(key)) or BreakerState()

    async def check(self, dimension: Dimension | None) -> str | None:
        """返回降级原因（OPEN 冷却内 / 半开超额），健康返回 None。

        C1：整个 load→冷却迁移→reload→判定在该维度的锁内完成，与 record 互斥。
        """
        dim = self._dim(dimension)
        async with self._lock_for(dim):
            st = await self._store.load(dim) or BreakerState()
            if st.status == BreakerStatus.OPEN.value:  # OPEN 态先判冷却：届满才尝试迁移，未届满维持拦截
                if self._clock() - (st.opened_at or 0) < self._cooldown:  # opened_at 缺失视为 0；冷却未过就继续熔断，给下游恢复窗口
                    return "breaker_open"
                await self._maybe_half_open_unlocked(dim)  # 已持锁，调无锁版避免死锁
                st = await self._store.load(dim) or BreakerState()
            if st.status == BreakerStatus.HALF_OPEN.value:  # 迁移后重新 load，按半开态的探测上限二次判定
                if len(st.window) >= self._max_probes:  # 探测数达上限即拦截，防止半开流量放大成真实压力
                    return "breaker_half_open"
            return None

    async def maybe_half_open(self, key: object) -> None:
        """公开 API：冷却期满 OPEN→HALF_OPEN（带锁，供外部/测试调用）。"""
        dim = _normalize_key(key)
        async with self._lock_for(dim):
            await self._maybe_half_open_unlocked(dim)

    async def _maybe_half_open_unlocked(self, dim: Dimension) -> None:
        """无锁版本：调用方须已持有该维度锁（check 内部复用，避免不可重入死锁）。"""
        st = await self._store.load(dim)
        if st is None or st.status != BreakerStatus.OPEN.value:  # 仅 OPEN 态需要迁移；无记录或已迁移则无事可做
            return
        if self._clock() - (st.opened_at or 0) >= self._cooldown:  # 冷却期满才半开：给下游恢复窗口，避免频繁抖动
            await self._store.save(dim, replace(
                st, status=BreakerStatus.HALF_OPEN.value, window=(),
                half_open_successes=0, opened_at=None,
            ))

    async def record(
        self, result: Result, dimension: Dimension | None = None, *, count: bool = True,
    ) -> None:
        """记录一次调用结果并推进三态机（锁内 load→decide→save，C1 防并发丢更新）。

        参数：
            result: 调用结果信封（ok 决定滑窗记成功/失败）。
            dimension: 熔断维度；None 用空维度。count: False 表示只探查不计数。
        """
        if not count:  # count=False 为纯探查：只解析维度不记账，避免观察动作污染统计
            return
        dim = self._dim(dimension)
        # C1：load→decide→save 全临界区加锁，异步 store 下并发不丢更新。
        async with self._lock_for(dim):
            await self._record_unlocked(dim, result.ok)

    async def _record_unlocked(self, dim: Dimension, success: bool) -> None:
        """无锁记录：调用方须已持该维度锁。"""
        st = await self._store.load(dim) or BreakerState()
        window = (st.window + (not success,))[-self._window_size:]  # 滑窗只留最近 window_size 次结果（True=失败），旧样本老化出窗
        failures = st.failures + (0 if success else 1)  # 累计失败数不清零，与滑窗一起供样本量判定
        if st.status == BreakerStatus.OPEN.value:  # OPEN 态不重新决策：在途请求回喂不得刷新 opened_at，否则冷却永不结束
            return
        if st.status == BreakerStatus.HALF_OPEN.value:  # 半开态走专用判定：用连续成功数决定回 CLOSED 还是再 OPEN，不看滑窗失败率
            if success:  # 探测成功只累加连续成功数，达标才恢复，防偶发成功误判已恢复
                successes = st.half_open_successes + 1
                if successes >= self._success_to_close:  # 连续成功达标：重置整份快照回 CLOSED，清掉半开计数
                    await self._store.save(dim, BreakerState())
                else:  # 未达标：保留半开态和滑窗，仅累加计数继续探测
                    await self._store.save(dim, replace(st, half_open_successes=successes))
            else:  # 探测失败即重新 OPEN：一次失败视为未恢复，刷新冷却起点重新计时
                await self._store.save(dim, replace(
                    st, status=BreakerStatus.OPEN.value, opened_at=self._clock(),
                    half_open_successes=0,
                ))
            return
        new_state = replace(st, window=window, failures=failures)
        if len(window) >= self._min_samples:  # 样本不足不算失败率，避免个别失败误触发熔断
            rate = sum(1 for bad in window if bad) / len(window)
            if rate >= self._threshold:  # 失败率越阈值即跳闸 OPEN，opened_at 作为后续冷却起点
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
        reason = await self.check(dim if isinstance(dim, tuple) else None)  # 自定义维度非二元组时回退空维度，避免污染 store key
        if reason:  # 有降级原因才发信号并占位 DEGRADE，具体目标交 settle 统一裁决
            failed_target = self._failed_target_of(effect)
            scope.emit(Signal(SignalKind.DEGRADE, reason, failed_target))
            return Decision.degrade("")
        return None

    async def observe(self, scope: RequestScope, effect: Effect) -> None:
        """after_invoke / invoke_error PARALLEL：从 scope.last_result 记录结果。"""
        result = scope.last_result
        if isinstance(result, Result):  # 只处理 Result 载体；其他形式（如守卫短路的 Decision）不参与记账
            dim = self._dimension_of(effect) if self._dimension_of else None
            await self.record(result, dim if isinstance(dim, tuple) else None)

    async def observe_error(self, scope: RequestScope, effect: Effect) -> None:
        """invoke_error 观察点：last_result 已由 host.call 写入，复用 observe 记账。"""
        await self.observe(scope, effect)
