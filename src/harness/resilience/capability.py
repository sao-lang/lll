"""R3 能力装配：ResilienceCapability 把显式选择的插件按 AOP 角色注册。

插件保留一等身份（name + 实例状态 + 构造注入），install 按角色分派到 AOPContainer。
装配顺序即执行顺序（同角色内按注册序）。
"""
from __future__ import annotations

from compose import AOPContainer, Envelope, Prelude, Refiner, Salvage, Warden

from .breaker import CircuitBreaker
from .defenses import (
    Bulkhead, Degradation, Idempotency, KillSwitch, RateLimit, Watchdog,
)
from .retry import RetryPolicy
from .timeout import Timeout

__all__ = ["ResilienceCapability"]


class ResilienceCapability:
    """可靠性能力包：把显式选择的插件装到 AOP 容器。"""
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
        timeout: Timeout | None = None,
        extra_plugins: tuple[object, ...] = (),
    ) -> None:
        """装配（opt-in）：传实例才装，不传即缺席。"""
        self._plugins = (
            killswitch, watchdog, idempotency, rate_limit, bulkhead,
            breaker, retry, timeout, degradation,
        )
        self._extra = tuple(extra_plugins)
        for p in self._extra:
            # 规则 6 例外：extra 插件是鸭子类型（非协议子类），装配期容错读取 name。
            if not getattr(p, "name", None):
                raise ValueError(f"extra plugin must have a name: {p!r}")

    def install(self, container: AOPContainer, reg: object | None = None) -> AOPContainer:
        """按角色注册插件（顺序即同角色内执行序）。

        角色映射：
            Warden：KillSwitch/Watchdog/Idempotency/RateLimit/CircuitBreaker。
            Envelope：Bulkhead/RetryPolicy/Timeout/CircuitBreaker（观察后置）。
            Salvage：Degradation。
        CircuitBreaker 同时注册 Warden（check）与 Envelope（observe），仍算一个插件。
        """
        for p in self.plugins():
            if isinstance(p, (KillSwitch, Watchdog, Idempotency, RateLimit)):
                container.add_warden(p)
            elif isinstance(p, CircuitBreaker):
                container.add_warden(p)  # check_and_signal
                container.add_envelope(p)  # observe 后置
            elif isinstance(p, (Bulkhead, RetryPolicy, Timeout)):
                container.add_envelope(p)
            elif isinstance(p, Degradation):
                container.add_salvage(p)
            elif isinstance(p, Prelude):
                container.add_prelude(p)
            elif isinstance(p, Warden):
                container.add_warden(p)
            elif isinstance(p, Envelope):
                container.add_envelope(p)
            elif isinstance(p, Refiner):
                container.add_refiner(p)
            elif isinstance(p, Salvage):
                container.add_salvage(p)
        return container

    def plugins(self) -> tuple[object, ...]:
        """已选插件（内置 + extra），顺序确定。"""
        return tuple(p for p in self._plugins if p is not None) + self._extra
