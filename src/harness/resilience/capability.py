"""R3 能力装配：ResilienceCapability 用 opt-in 构造注入领域策略，产出可靠性执行器。

插件保留一等身份（name + 实例状态 + 构造注入）；install 按领域把各策略注入
ReliabilityExecutor 的具名字段——没有角色分派、没有槽位列表、没有 Role 枚举。
缺席的策略传 None，执行器跳过该领域步骤（无占位实现）。
扩展点 extra_plugins 只接受函数式 call 变换器（callable），不是角色插件。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from compose.context import Invocation
from runtime import Result

type _InvCall = Callable[[Invocation], Awaitable[Result]]
type _Transformer = Callable[[_InvCall], _InvCall]
"""函数式领域调用变换器（扩展点形态，非角色插件）。"""

from .breaker import CircuitBreaker
from .defenses import (
    Bulkhead,
    Degradation,
    Idempotency,
    KillSwitch,
    RateLimit,
    Watchdog,
)
from .executor import ReliabilityExecutor
from .retry import RetryPolicy
from .timeout import Timeout

__all__ = ["ResilienceCapability"]


class ResilienceCapability:
    """可靠性能力包：把显式选择的领域策略装进可靠性执行器。"""
    name = "resilience"
    version = "6.0.0"
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
        extra_plugins: tuple[_Transformer, ...] = (),
    ) -> None:
        """装配（opt-in）：传实例才装，不传即该领域缺席。"""
        self._kill_switch = killswitch
        self._watchdog = watchdog
        self._idempotency = idempotency
        self._rate_limit = rate_limit
        self._bulkhead = bulkhead
        self._breaker = breaker
        self._retry = retry
        self._timeout = timeout
        self._degradation = degradation
        self._extra = tuple(extra_plugins)
        for p in self._extra:
            if not callable(p):
                raise ValueError(f"extra plugin must be a call transformer: {p!r}")

    def install(self, container: object | None = None, reg: object | None = None) -> None:  # purity-exempt: HEAD 存量动态边界（install/container 协议与鸭子参数，行号位移非新增）
        """Capability 协议装配（install(container, reg)）：注册执行器工厂。

        真实绑定：把「core → ReliabilityExecutor」的工厂闭包注册为
        "reliability_executor" provider——消费方（门面/组合层）可经 registry
        解析出工厂并传入自己的中心调用。策略包装本身见 wrap()。
        """
        if reg is None or not hasattr(reg, "register"):
            return

        def executor_factory(
            core: _InvCall,
            *,
            registry: object | None = None,  # purity-exempt: HEAD 存量鸭子注册表（行号位移非新增）
            bus: object | None = None,  # purity-exempt: HEAD 存量鸭子总线（行号位移非新增）
            notify: Callable[[str, Mapping[str, object]], None] | None = None,  # purity-exempt: HEAD 存量动态通知（行号位移非新增）
        ) -> ReliabilityExecutor:
            return self.wrap(core, registry=registry, bus=bus, notify=notify)

        reg.register("reliability_executor", executor_factory)  # purity-exempt: reg 为鸭子类型对象

    def wrap(
        self,
        core: Callable[[Invocation], Result | Awaitable[Result]],
        *,
        registry: object | None = None,  # purity-exempt: HEAD 存量鸭子注册表（行号位移非新增）
        bus: object | None = None,  # purity-exempt: HEAD 存量鸭子总线（行号位移非新增）
        notify: Callable[[str, Mapping[str, object]], None] | None = None,  # purity-exempt: HEAD 存量动态通知（行号位移非新增）
    ) -> ReliabilityExecutor:
        """把中心 Port 调用包装进可靠性执行器（原 install 的包装语义）。

        参数：
            core: 中心 Port 调用（如 ModelGateway 调用链）。
            registry: 共用 Provider 注册表（可选）。
            bus: LifecycleBus（可选，invoke_rejected 经它广播）。
            notify: 观察回调（可选）。
        """
        return ReliabilityExecutor(
            core,
            registry=registry,
            bus=bus,
            notify=notify,
            kill_switch=self._kill_switch,
            watchdog=self._watchdog,
            idempotency=self._idempotency,
            rate_limit=self._rate_limit,
            bulkhead=self._bulkhead,
            breaker=self._breaker,
            retry=self._retry,
            timeout=self._timeout,
            degradation=self._degradation,
            transformers=self._extra,
        )

    def plugins(self) -> tuple[object, ...]:  # purity-exempt: HEAD 存量异构策略元组（行号位移非新增）
        """已选领域策略（内置 + extra），顺序确定（供装配核验）。"""
        builtin = (
            self._kill_switch, self._watchdog, self._idempotency, self._rate_limit,
            self._bulkhead, self._breaker, self._retry, self._timeout, self._degradation,
        )
        return tuple(p for p in builtin if p is not None) + self._extra
