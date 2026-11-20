"""AOP 增强体系：五角色增强器 + 固定顺序编排容器。

三体系正交中的 "how" 维度（返回值被消费）：Prelude/Warden/Envelope/Refiner/Salvage
共享单一上下文 Invocation；执行顺序由容器固定，增强器不声明 priority。
"""
from __future__ import annotations

from .container import AOPContainer
from .invocation import (
    FAULT_KEY,
    ROUTING_TARGET_KEY,
    FaultRecord,
    Invocation,
    Next,
    RoutingTarget,
    Verdict,
)
from .protocols import (
    ROLE_NAMES,
    Envelope,
    FailureClass,
    Prelude,
    Refiner,
    Role,
    Salvage,
    Warden,
)

__all__ = [
    "AOPContainer",
    "Invocation", "Next", "Verdict", "FaultRecord", "RoutingTarget",
    "FAULT_KEY", "ROUTING_TARGET_KEY",
    "Prelude", "Warden", "Envelope", "Refiner", "Salvage", "FailureClass", "Role", "ROLE_NAMES",
]
