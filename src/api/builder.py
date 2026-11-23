"""R11 A3 应用开发装配器：在 R12 Agent 之上加 profile + 默认能力装配。

依赖方向：api(L3) → compose/archs/harness(L1/L2) → runtime(L0)；不改 R5/R9 源码。
R9 三 Port 默认装配在此落地（组合层跨模块装配），无注入保持 fail-closed。
"""
from __future__ import annotations

from compose import Agent, Capability, Registry
from harness.security import SecurityCapability
from runtime import Session

from .profiles import PROFILES
from .static_api import static_check_api
from .governance import Governance

__all__ = ["AgentBuilder"]

type _RegisteredCapability = Capability | str  # 真能力或缺席能力名（纯数据，不是空实现）


class AgentBuilder:
    """装配门面：持有 R12 Agent，按 profile 装能力，build 产 Session。"""
    name = "agent_builder"
    version = "11.0.0"

    def __init__(self, arch: object) -> None:  # purity-exempt: arch 为 Arch 协议形状，api 不反向依赖 archs
        """构造装配器。

        Args:
            arch: Arch 或 Program 工厂（交 R12 Agent 处理）。
        """
        self._agent = Agent(arch)
        self._profiles: list[str] = []
        self._caps: dict[str, _RegisteredCapability] = {}
        self._security: SecurityCapability | None = None
        self._governance: Governance | None = None

    def profile(self, name: str) -> "AgentBuilder":
        """应用 profile 预设（装配期按清单装能力，未知名拒绝）。"""
        if name not in PROFILES:
            raise ValueError(f"unknown profile: {name}")
        self._profiles.append(name)
        for cap_name in PROFILES[name]:
            self._install_named_capability(cap_name)
        return self

    def _install_named_capability(self, cap_name: str) -> None:
        """按能力名装配（只登记，不实现能力）。"""
        if cap_name == "security":
            sec = SecurityCapability()
            self._security = sec
            self._caps["security"] = sec
            sec.install(self._agent, self._agent.registry())
        elif cap_name == "audit":
            # regulated 档：审计已由 security 的 AuditLogger 提供，显式标记。
            self._caps.setdefault("audit", self._security or SecurityCapability())
        else:
            # resilience/rag/observability 由各自 Capability 实现；此处登记占位名，
            # 真实注入由调用方 capability() 提供（缺席即降级，仅登记名字非实现）。  # purity-exempt: 缺席能力用名字表达，无空实现
            self._caps.setdefault(cap_name, cap_name)  # 缺席：仅登记名字，不造空实现

    def capability(self, cap: Capability) -> "AgentBuilder":
        """注入自定义能力（覆盖同名预设）。"""
        self._caps[cap.name] = cap
        self._agent.capability(cap)
        return self

    def applied_profiles(self) -> tuple[str, ...]:
        """已应用 profile 快照。"""
        return tuple(self._profiles)

    def capability_names(self) -> tuple[str, ...]:
        """已登记能力名快照。"""
        return tuple(self._caps)

    def registry(self) -> Registry:
        """返回共用 Registry（R9 三 Port 由 SecurityCapability 注册）。"""
        return self._agent.registry()

    def govern(self, gov: Governance) -> "AgentBuilder":
        """注入治理门面；build 时真调用 gov.allow（不静默忽略）。"""
        self._governance = gov
        return self

    def build(self) -> Session:
        """治理判定 + 装配期检查 + 产 Session（委托 R12 Agent）。"""
        if self._governance is not None and not self._governance.allow("session.build"):
            raise ValueError("governance denied: session.build")
        report = static_check_api(getattr(self._agent, "_arch", None))
        if not report.ok:
            codes = ", ".join(str(e.code) for e in report.errors)
            raise ValueError(f"static_check failed: {codes}")
        return self._agent.build()
