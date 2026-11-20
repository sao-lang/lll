"""协作 DTO 归位（规格 P2）：Invocation/FaultRecord/RoutingTarget/Verdict/Next 移入组合层。

这些是守卫/装饰器协作 DTO，不进 R1（R1 明确不构造 Invocation）。原在 src/aop/invocation.py，
本次归位到组合层——这不是兼容层，是规格 P2 的正式迁移。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from runtime import Effect, Result

from .call import Verdict
from .guards import FaultRecord, RoutingTarget

__all__ = [
    "CallContext", "Invocation", "Next", "Verdict",
    "FaultRecord", "RoutingTarget", "FAULT_KEY", "ROUTING_TARGET_KEY",
]


FAULT_KEY: Final[str] = "fault"
ROUTING_TARGET_KEY: Final[str] = "routing_target"


@dataclass
class CallContext:
    """组合层调用上下文（原 Invocation 归位）：effect + ctx + session + 具名 meta。

    字段：
        effect: 当前 Effect（Invoke 类）。
        ctx: R1 RuntimeCtx（或测试最小 ctx）。
        session: 会话视图（含 session_id，B1 隔离维度来源）。
        meta: 具名协作数据（fault/routing_target），守卫与装饰器共享同一份。
    """
    effect: Effect
    ctx: object
    session: object
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        """从 session 派生会话标识（B1 选级隔离维度单一入口）。"""
        return str(getattr(self.session, "session_id", "") or "")

    def write_fault(self, fault: FaultRecord) -> None:
        """写入具名故障记录。"""
        self.meta[FAULT_KEY] = fault

    def fault(self) -> FaultRecord | None:
        """读取当前故障记录；无则 None。"""
        value = self.meta.get(FAULT_KEY)
        return value if isinstance(value, FaultRecord) else None

    def write_routing_target(self, target: RoutingTarget) -> None:
        """写入 typed 降级目标。"""
        self.meta[ROUTING_TARGET_KEY] = target

    def routing_target(self) -> RoutingTarget | None:
        """读取 typed 降级目标；无则 None。"""
        value = self.meta.get(ROUTING_TARGET_KEY)
        return value if isinstance(value, RoutingTarget) else None

    def meta_view(self) -> Mapping[str, object]:
        """返回 meta 只读快照（供 notify 投影，不暴露可变字典）。"""
        return MappingProxyType(dict(self.meta))


# Invocation 是 CallContext 的归位别名（源码/测试迁移期统一命名；非兼容层，是同一 DTO）
Invocation = CallContext

type Next = Callable[[CallContext], Awaitable[Result]]
"""增强链下一跳签名（Envelope/Refiner 等包裹中心调用时用）。"""
