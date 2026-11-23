"""R11 反思能力包：ReflectStrategy 的可插拔装配（use() / 架构 / 组合三路）。

本包不实现反思循环（在 archs.Reflection/loop_until 里），只做：
- 履约 Capability 协议（name/version/requires/install）；
- 持策略对象并转调 critique(draft, goal, ctx)（三参真传）；
- 供门面 evaluate() 与 Reflection 架构消费同一策略实例。
"""
from __future__ import annotations

from runtime import AgentState
from compose import Registry

from .contracts import Critique, ReflectStrategy
from .services import ThresholdReflector

__all__ = ["ReflectionCapability"]


class ReflectionCapability:
    """反思能力包：持有 ReflectStrategy（缺省阈值评审器），三路可组装。"""

    name = "reflection"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, *, reflector: ReflectStrategy | None = None) -> None:
        """构造：缺省 ThresholdReflector（本地确定性）。"""
        self.reflector: ReflectStrategy = reflector or ThresholdReflector()

    def critique(self, draft: str, goal: str = "",
                 ctx: AgentState | None = None) -> Critique:
        """评审转调：三参（draft/goal/ctx）真传给注入策略。"""
        return self.reflector.critique(draft, goal, ctx)

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: "Registry | None" = None) -> None:
        """Capability 协议：注册反思策略为一等 provider。"""
        if reg is not None:
            reg.register("reflection", self)
