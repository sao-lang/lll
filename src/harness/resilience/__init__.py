"""R3 可靠性能力插件：失败分类 / 重试 / 熔断 / 降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。

全部基于 R1（runtime）+ R2（hooks）构建，以 Hook 插件形式挂载，内核零策略（R3 §1）。
公共 API 见 docs/02-模块详设/Agent/R3-可靠性.md §4。
"""
from __future__ import annotations

from .breaker import CircuitBreaker
from .capability import ResilienceCapability
from .defenses import Bulkhead, Degradation, Idempotency, KillSwitch, RateLimit, Watchdog
from .retry import RetryPolicy
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
    "DegradeMode", "DegradeStep", "DegradeLadder", "Jitter",
    "BreakerStatus", "BreakerState",
    # 插件
    "RetryPolicy", "CircuitBreaker", "Degradation", "RateLimit", "Bulkhead",
    "Idempotency", "Watchdog", "KillSwitch", "ResilienceCapability",
]
