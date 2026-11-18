"""R3 能力装配：ResilienceCapability.install(host) 挂载显式选择的插件。

ARCH-v5：插件只实现 apply(host)，状态由插件实例自身持有（跨调用存活），
不自声明 mode/priority/events；装配顺序即 tap 顺序（宿主扩展点内有序）。
"""
from __future__ import annotations

from hooks import Host

from .breaker import CircuitBreaker
from .defenses import (
    BudgetGuard, Bulkhead, Degradation, Idempotency, KillSwitch, RateLimit, Watchdog,
)
from .retry import RetryPolicy
from .timeout import Timeout

__all__ = ["ResilienceCapability"]


class ResilienceCapability:
    """可靠性能力包：把显式选择的插件装到 host。"""

    name = "resilience"
    version = "5.0.0"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
        rate_limit: RateLimit | None = None,
        bulkhead: Bulkhead | None = None,
        watchdog: Watchdog | None = None,
        killswitch: KillSwitch | None = None,
        idempotency: Idempotency | None = None,
        degradation: Degradation | None = None,
        budget_guard: BudgetGuard | None = None,
        timeout: Timeout | None = None,
        extra_plugins: tuple[object, ...] = (),
    ) -> None:
        """装配（opt-in）：传实例才装，不传即缺席。"""
        self._plugins = (
            killswitch, watchdog, idempotency, bulkhead, rate_limit, breaker,
            retry, timeout, degradation, budget_guard,
        )
        self._extra = tuple(extra_plugins)
        for p in self._extra:  # 装配期逐个校验额外插件，fail-fast 避免运行期遇到无名插件
            # 规则 6 例外：extra 插件是鸭子类型（非协议子类），装配期以 getattr 容错读取 name。
            if not getattr(p, "name", None):
                raise ValueError(f"extra plugin must have a name: {p!r}")

    def install(self, host: Host) -> Host:
        """注册插件（顺序即 tap 顺序）。"""
        for p in self.plugins():  # 按序注册：tap 顺序即插件执行顺序（守卫类在前、降级兜底在后）
            host.plugin(p)  # type: ignore[arg-type]
        return host

    def plugins(self) -> tuple[object, ...]:
        """已选插件（内置 + extra），顺序确定。"""
        return tuple(p for p in self._plugins if p is not None) + self._extra  # 过滤未装配的 None 槽位；extra 追加在后
