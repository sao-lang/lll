"""R3 能力装配：ResilienceCapability.install(bus) 一键挂载五道防线。

三层插拔模型（R13）的能力级开关：装了通过 install 生效，不装即缺席（降级）。
插件顺序由 priority 保证确定性：trace 里按 priority 留名，可回放、可断言。
"""
from __future__ import annotations

from collections.abc import Callable
from random import Random

from hooks import HookBus, Registry

from .breaker import CircuitBreaker
from .defenses import (
    BudgetGuard,
    Bulkhead,
    Degradation,
    Idempotency,
    KillSwitch,
    RateLimit,
    Watchdog,
)
from .retry import RetryPolicy
from .types import DegradeLadder, DegradeMode, DegradeStep

__all__ = ["ResilienceCapability"]


class ResilienceCapability:
    """可靠性能力包：把五道防线按 priority 装到 HookBus。

    属性：
        name/version/requires: 能力级元数据（R13 Capability 契约）。
    """

    name = "resilience"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        ladder: DegradeLadder | None = None,
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
        rate_limit: RateLimit | None = None,
        bulkhead: Bulkhead | None = None,
        watchdog: Watchdog | None = None,
        killswitch: KillSwitch | None = None,
        idempotency: Idempotency | None = None,
        degradation: Degradation | None = None,
        budget_guard: BudgetGuard | None = None,
        clock: Callable[[], float] | None = None,
        rng: Random | None = None,
    ) -> None:
        """装配能力：可注入自定义插件/策略，缺省用默认实现。

        参数：
            ladder: 降级阶梯（缺省 LLM 默认阶梯）。
            breaker/retry/rate_limit/bulkhead/watchdog/killswitch/idempotency: 自定义插件实例。
            degradation/budget_guard: 自定义降级/软预算守卫实例（缺省由 ladder / 默认阈值构造）。
            clock/rng: 注入时钟与随机源（传给默认插件）。
        """
        self._ladder = ladder or _default_ladder()
        self._breaker = breaker
        self._retry = retry
        self._rate_limit = rate_limit
        self._bulkhead = bulkhead
        self._watchdog = watchdog
        self._killswitch = killswitch
        self._idempotency = idempotency
        self._degradation = degradation
        self._budget_guard = budget_guard
        self._clock = clock
        self._rng = rng

    def hooks(self) -> list[object]:
        """返回按 priority 排序的插件列表（供 trace 顺序断言与 bus 注册）。

        返回：
            插件实例列表（从小到大 priority）。
        """
        items: list[object] = [
            self._killswitch or KillSwitch(flag_store={}),
            self._watchdog or Watchdog(),
            self._idempotency or Idempotency(),
            self._bulkhead or Bulkhead(),
            self._rate_limit or RateLimit(clock=self._clock),
            self._breaker or CircuitBreaker(clock=self._clock),
            self._retry or RetryPolicy(rng=self._rng),
            self._degradation or Degradation(self._ladder),
            self._budget_guard or BudgetGuard(),
        ]
        # 规则 6 例外：装配期对鸭子插件的容错（自定义插件可能非 Hook 子类，
        # 只约定带 priority 属性），故在此用 getattr 兜底 100；运行期无此动态读取。
        return sorted(items, key=lambda h: getattr(h, "priority", 100))

    def install(self, bus: HookBus, reg: Registry | None = None) -> HookBus:
        """把全部插件装到总线（能力级插拔入口）。

        参数：
            bus: 目标 HookBus。reg: Provider 注册表（预留，当前插件无需 port）。
        返回：
            同一 bus（链式调用）。
        """
        for hook in self.hooks():
            bus.on(hook)  # type: ignore[arg-type]
        return bus


def _default_ladder() -> DegradeLadder:
    """LLM 默认降级阶梯：主模型→备模型→语义缓存→拒答（quality 递减、无环）。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="cache", mode=DegradeMode.CACHE, quality=0.3),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))
