"""R3 可靠性能力：失败分类 / 重试 / 熔断 / 降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。

每个策略以自身领域方法参与，由 ReliabilityExecutor 显式编排（无编排角色、无角色协议）；
全部基于 R1（runtime）构造注入，内核零策略（R3 §1）。
公共 API 见 docs/02-模块详设/Agent/R3-可靠性.md §4。
"""
from __future__ import annotations

from .breaker import BreakerStore, CircuitBreaker, InMemoryBreakerStore
from .capability import ResilienceCapability
from .defenses import (
    Bulkhead, Degradation, Idempotency,
    KillSwitch, RatePermit, RateLimit, Watchdog,
)
from .executor import ReliabilityExecutor
from .retry import RetryPolicy
from .timeout import Timeout
from .types import (
    DEFAULT_FAILURE_TABLE,
    BreakerState,
    BreakerStatus,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    DimensionKey,
    FailureClass,
    FailureClassifier,
    Jitter,
    RetrySpec,
    backoff_delay,
    classify_failure,
    normalize_code,
    validate_ladder,
    validate_retry_spec,
)

__all__ = [
    # 共享类型
    "FailureClass", "FailureClassifier", "DimensionKey", "DEFAULT_FAILURE_TABLE",
    "backoff_delay", "classify_failure", "normalize_code",
    "validate_retry_spec", "validate_ladder",
    "DegradeMode", "DegradeStep", "DegradeLadder", "Jitter", "RetrySpec",
    "BreakerStatus", "BreakerState", "BreakerStore", "InMemoryBreakerStore",
    # 插件（RetryPolicy 是重试策略配置，非状态 Policy）
    "RetryPolicy", "CircuitBreaker", "Degradation", "RateLimit", "Bulkhead",
    "Idempotency", "Watchdog", "KillSwitch", "ResilienceCapability",
    "Timeout", "RatePermit", "ReliabilityExecutor",
]
