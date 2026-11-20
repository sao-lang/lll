"""AOP 增强体系的统一调用上下文与具名数据载体。

Invocation 是五角色增强器共享的单一上下文（effect/ctx/session/meta）；
meta 承载请求期协作的具名记录（故障记录、typed 降级目标），不散落裸键。
Notice 是 Invocation 的只读投影（见 hooks/lifecycle.py）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from runtime import Effect, Result

__all__ = [
    "Invocation",
    "Next",
    "Verdict",
    "FaultRecord",
    "RoutingTarget",
    "FAULT_KEY",
    "ROUTING_TARGET_KEY",
]


FAULT_KEY = "fault"
"""Invocation.meta 中具名故障记录的键（替代旧信号链）。"""
ROUTING_TARGET_KEY = "routing_target"
"""Invocation.meta 中 typed 降级目标的键（R3 产出 → R4 消费）。"""


@dataclass(frozen=True)
class FaultRecord:
    """具名故障记录（四坑语义载体，替代旧 Signal 链）。

    字段：
        reason: 故障原因（rate_limited/retry_exhausted/timeout/breaker_open...），只进 trace。
        failed_target: 降级阶梯里的后端注册名（B2：非熔断维度二元组；R3-7：retry/timeout 必带）。
    """
    reason: str
    failed_target: str = ""


@dataclass(frozen=True)
class RoutingTarget:
    """typed 降级目标（R3 Degradation 产出 → R4 网关消费，与 Registry 同源）。

    字段：
        target: 后端注册名或 "refuse"。
        reason: 降级原因（只进 trace，不参与选后端）。
    """
    target: str
    reason: str = ""


@dataclass(frozen=True)
class Verdict:
    """Warden 通用裁决：allowed=False 短路拒绝；reason 进 invoke_rejected 通知。

    安全域四态（allow/redact/deny/ask）由 R9 的 SafetyVerdict 承担，不复用此类。
    """
    allowed: bool = True
    reason: str = ""


@dataclass
class Invocation:
    """五角色增强器共享的单一调用上下文。

    字段：
        effect: 当前 Effect（具体为 Invoke，唯一进 AOP 链的成员）。
        ctx: RuntimeCtx（Runner 构造、含 invoke 回调）。
        session: Session（会话，含 session_id；B1 选级的隔离维度来源）。
        meta: 请求期具名协作数据（fault / routing_target 等），装配层与增强器共享同一份。
    """
    effect: Effect
    ctx: Any
    session: Any
    meta: dict[str, Any]

    @property
    def session_id(self) -> str:
        """从 session 派生会话标识（B1 选级隔离维度的单一入口）。"""
        return str(getattr(self.session, "session_id", "") or "")

    def write_fault(self, fault: FaultRecord) -> None:
        """写入具名故障记录（Warden/Envelope 报告故障的唯一入口）。

        参数：
            fault: 故障记录（reason + failed_target）。
        """
        self.meta[FAULT_KEY] = fault

    def fault(self) -> FaultRecord | None:
        """读取当前故障记录；无则返回 None。"""
        value = self.meta.get(FAULT_KEY)
        return value if isinstance(value, FaultRecord) else None

    def write_routing_target(self, target: RoutingTarget) -> None:
        """写入 typed 降级目标（Degradation 裁决后、网关消费前）。

        参数：
            target: 降级目标记录。
        """
        self.meta[ROUTING_TARGET_KEY] = target

    def routing_target(self) -> RoutingTarget | None:
        """读取 typed 降级目标；无则返回 None。"""
        value = self.meta.get(ROUTING_TARGET_KEY)
        return value if isinstance(value, RoutingTarget) else None

    def meta_view(self) -> Mapping[str, Any]:
        """返回 meta 的只读快照（供 Notice 投影，避免暴露可变字典）。"""
        return MappingProxyType(dict(self.meta))


type Next = Callable[[Invocation], Awaitable[Result]]
"""增强链下一跳签名：Prelude/Envelope 收 next 包裹中心调用。"""
