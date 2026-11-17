"""R3 熔断器：三态机（closed/open/half_open）+ 滑窗失败率 + 维度隔离。

熔断状态按维度隔离（provider/model/tool/tenant），避免一个模型故障拖垮全部。
时钟显式注入（clock: () -> float），不用墙钟，保证状态迁移可测试、可回放。
多实例部署需注入共享 store（构造参数），默认进程内字典仅用于单实例闭环。
"""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import replace

from hooks import Decision, HookCtx, HookMode, LifecycleEvent
from runtime import Result

from harness._ctxview import Dimension, coerce_ctx

from .types import BreakerState, BreakerStatus

__all__ = ["CircuitBreaker"]

_EMPTY: Dimension = ("", "")


class CircuitBreaker:
    """熔断器插件：GUARD 检查状态（open→DEGRADE）；AROUND 后置记录结果。

    三态机：
        CLOSED：样本足够 + 失败率≥阈值 → OPEN。
        OPEN：冷却期届满 → HALF_OPEN；否则 DEGRADE 拦截。
        HALF_OPEN：放行有限探测；连续 N 次成功 → CLOSED，任一失败 → OPEN。
    """

    name = "r3_breaker"
    events = ("before_invoke", "after_invoke", "invoke_error")
    mode = HookMode.GUARD
    priority = 40  # killswitch→bulkhead→ratelimit→breaker→degrade（R3 §2）
    requires_ports: tuple[str, ...] = ()

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
        store: MutableMapping[Dimension, BreakerState] | None = None,
    ) -> None:
        """装配熔断器参数。

        参数：
            failure_rate: 触发 open 的失败率阈值。minimum_samples: 最小样本数（不足绝不 open）。
            window_size: 滑窗大小。open_cooldown_s: open→half_open 冷却秒数。
            half_open_max_probes: 半开最大探测数。half_open_success_to_close: 连续成功数阈值。
            per_dimension: 是否按维度隔离（False 时全部归并到空维度）。
            clock: 注入时钟（秒）。store: 共享状态存储（多实例部署注入分布式 store 适配）。
        """
        self._threshold = failure_rate
        self._min_samples = minimum_samples
        self._window_size = window_size
        self._cooldown = open_cooldown_s
        self._max_probes = half_open_max_probes
        self._success_to_close = half_open_success_to_close
        self._per_dimension = per_dimension
        self._clock = clock or (lambda: 0.0)
        self._store = store if store is not None else {}

    def _dim(self, dimension: Dimension | None) -> Dimension:
        """归一化维度键：per_dimension=False 时全部归并到空维度。"""
        if not self._per_dimension:
            return _EMPTY
        return dimension if dimension is not None else _EMPTY

    def state(self, key: object) -> BreakerState:
        """读取某维度的状态快照（不存在的维度返回 CLOSED 默认态）。

        参数：
            key: 维度键（tuple 或空串）。
        返回：
            BreakerState 快照。
        """
        dim = self._normalize_key(key)
        return self._store.get(dim, BreakerState())

    @staticmethod
    def _normalize_key(key: object) -> Dimension:
        """把测试传入的维度键（tuple / str）归一为 Dimension。"""
        if isinstance(key, tuple) and len(key) == 2:
            return (str(key[0]), str(key[1]))
        return _EMPTY

    def check(self, ctx: HookCtx) -> Decision:
        """GUARD 检查：OPEN 且冷却未满 → DEGRADE；半开超额 → DEGRADE。"""
        dim = self._dim_from_ctx(ctx)
        st = self.state(dim)
        if st.status == BreakerStatus.OPEN.value:
            if self._clock() - (st.opened_at or 0) < self._cooldown:
                ctx.trace.append("breaker:open:degrade")
                return Decision.degrade("breaker_open")
            self.maybe_half_open(dim)
            st = self.state(dim)
        if st.status == BreakerStatus.HALF_OPEN.value:
            probes = len(st.window)  # 半开窗口长度即已放行探测数
            if probes >= self._max_probes:
                ctx.trace.append("breaker:half_open:probe_limit")
                return Decision.degrade("breaker_half_open")
        return Decision.continue_()

    def _dim_from_ctx(self, ctx: HookCtx) -> Dimension:
        """从具名视图读取显式维度（装配层写入）；无则空维度。

        动态读取已收敛到 coerce_ctx（规则 8），此处只做属性访问。
        不自动从 Effect.payload 推断 model：推理维度由装配层决定（模型路由/工具），
        熔断器只认结构化维度键，避免把 payload 动态键误当隔离维度。
        """
        return coerce_ctx(ctx).breaker_dimension or _EMPTY

    def maybe_half_open(self, key: object) -> None:
        """冷却期届满时把 OPEN 迁移到 HALF_OPEN（重置探测窗）。"""
        dim = self._normalize_key(key)
        st = self._store.get(dim)
        if st is None or st.status != BreakerStatus.OPEN.value:
            return
        if self._clock() - (st.opened_at or 0) >= self._cooldown:
            self._store[dim] = replace(
                st, status=BreakerStatus.HALF_OPEN.value, window=(),
                half_open_successes=0, opened_at=None,
            )

    def record(
        self, result: Result, ctx: HookCtx, *, dimension: Dimension | None = None,
        count: bool = True,
    ) -> None:
        """记录一次调用结果并推进三态机。

        参数：
            result: 调用结果。ctx: 上下文（用于 trace）。
            dimension: 显式维度；None 时从 ctx 推断。
            count: 是否计入滑窗（429 等对方保护类失败传 False）。
        """
        dim = self._dim(dimension if dimension is not None else self._dim_from_ctx(ctx))
        st = self._store.get(dim, BreakerState())
        if not count:
            ctx.trace.append("breaker:not_counted")
            return
        success = result.ok
        window = (st.window + (not success,))[-self._window_size:]
        failures = st.failures + (0 if success else 1)

        if st.status == BreakerStatus.OPEN.value:
            return  # open 态由 check/maybe_half_open 驱动迁移
        if st.status == BreakerStatus.HALF_OPEN.value:
            if success:
                successes = st.half_open_successes + 1
                if successes >= self._success_to_close:
                    self._store[dim] = BreakerState()
                    ctx.trace.append("breaker:half_open:closed")
                else:
                    self._store[dim] = replace(st, half_open_successes=successes)
            else:
                self._store[dim] = replace(
                    st, status=BreakerStatus.OPEN.value, opened_at=self._clock(),
                    half_open_successes=0,
                )
                ctx.trace.append("breaker:half_open:reopened")
            return

        # CLOSED：样本足够 + 失败率≥阈值 → OPEN
        new_state = replace(st, window=window, failures=failures)
        if len(window) >= self._min_samples:
            rate = sum(1 for bad in window if bad) / len(window)
            if rate >= self._threshold:
                new_state = replace(
                    new_state, status=BreakerStatus.OPEN.value, opened_at=self._clock()
                )
                ctx.trace.append("breaker:open")
        self._store[dim] = new_state

    async def __call__(self, ctx: HookCtx) -> Decision:
        """Hook 入口：invoke_error 事件记录失败，其余事件做拦截检查。"""
        if ctx.event == LifecycleEvent.INVOKE_ERROR.value:
            # last_result 由 coerce_ctx 具名视图提供（规则 8，不再散落 meta.get）。
            result = coerce_ctx(ctx).last_result
            if isinstance(result, Result):
                # 失败是否计入熔断由失败分类决定；这里保守计入（429 由上层传 count=False）。
                self.record(result, ctx)
            return Decision.continue_()
        return self.check(ctx)
