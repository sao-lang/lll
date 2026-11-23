"""R11 集成接入与治理（api）：四组 API 投影 + 装配 + 治理 + CLI。

只做投影/装配/治理，不实现任何具体能力（能力在 R3~R10）；
依赖只向下：compose/archs/harness → runtime。
"""
from __future__ import annotations

from .profiles import PROFILES, profile_names
from .ports import PortRegistry, PROTOCOL_NAMES
from .stream import StreamGateway
from .static_api import static_check_api
from .governance import KillSwitch, CostLimiter, Rollout, FeatureFlags, Governance
from .builder import AgentBuilder
from .cli import CLI, CLIResult

__all__ = [
    "PROFILES", "profile_names",
    "PortRegistry", "PROTOCOL_NAMES",
    "StreamGateway", "static_check_api",
    "KillSwitch", "CostLimiter", "Rollout", "FeatureFlags", "Governance",
    "AgentBuilder", "CLI", "CLIResult",
]
