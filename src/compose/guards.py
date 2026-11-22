"""具名故障 / typed 目标 DTO 与 call 层协作状态（领域数据，非拦截协议）。

四坑语义（B1/B2/R3-6/R3-7）的载体：从 Invocation.meta 改为 CallState(ctx.meta)。
本模块只有数据结构与读写，不含任何 check/observe 拦截逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["FaultRecord", "RoutingTarget", "CallState"]


class _Keys:
    """ctx.meta 具名协作键（替代旧 Invocation.meta 裸键），集中定义禁用魔法字符串。"""
    FAULT: Final[str] = "fault"
    ROUTING_TARGET: Final[str] = "routing_target"


@dataclass(frozen=True, slots=True)
class FaultRecord:
    """具名故障记录（四坑语义载体）。

    Attributes:
        reason: 故障原因（rate_limited/retry_exhausted/timeout/breaker_open...），只进 trace。
        failed_target: 降级阶梯后端注册名（B2：非熔断维度二元组；R3-7 retry/timeout 必带）。
    """
    reason: str
    failed_target: str = ""


@dataclass(frozen=True, slots=True)
class RoutingTarget:
    """typed 降级目标（②守卫裁决 → 网关消费，与 Registry 同源）。

    Attributes:
        target: 后端注册名或 "refuse"。
        reason: 降级原因（只进 trace，不参与选后端）。
    """
    target: str
    reason: str = ""


class CallState:
    """call 层具名协作状态：从 ctx.meta 读写 fault / routing_target（替代 Invocation.meta）。

    状态存在调用上下文（R1 RuntimeCtx.meta）；可靠性执行器与各领域策略
    共享同一份 meta（B3 状态统一），单次流程进度不进策略实例（D-8）。
    """
    def __init__(self, ctx: object) -> None:
        """绑定调用上下文。

        Args:
            ctx: R1 RuntimeCtx（含 meta 字典）或测试最小 ctx。
        """
        self._ctx = ctx

    def _meta(self) -> dict[str, object]:
        """取 ctx.meta 字典（缺省构造空字典挂回 ctx，保证读写同源）。"""
        meta = getattr(self._ctx, "meta", None)
        if not isinstance(meta, dict):
            meta = {}
            try:
                setattr(self._ctx, "meta", meta)
            except (AttributeError, TypeError):  # ctx 不可写（如 frozen）：仅本地读写
                pass
        return meta

    def write_fault(self, fault: FaultRecord) -> None:
        """写入具名故障记录（领域策略报告故障的唯一入口）。"""
        self._meta()[_Keys.FAULT] = fault

    def fault(self) -> FaultRecord | None:
        """读取当前故障记录；无或类型不符返回 None。"""
        value = self._meta().get(_Keys.FAULT)
        return value if isinstance(value, FaultRecord) else None

    def write_routing_target(self, target: RoutingTarget) -> None:
        """写入 typed 降级目标（降级选级后、网关消费前）。"""
        self._meta()[_Keys.ROUTING_TARGET] = target

    def routing_target(self) -> RoutingTarget | None:
        """读取 typed 降级目标；无或类型不符返回 None。"""
        value = self._meta().get(_Keys.ROUTING_TARGET)
        return value if isinstance(value, RoutingTarget) else None

    def session_id(self) -> str:
        """从 ctx 派生会话标识（B1 选级隔离维度的单一入口）。"""
        return str(getattr(self._ctx, "session_id", "") or "")
