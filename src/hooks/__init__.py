"""R2 可插拔协议与 Port 契约（三体系正交的 what 维度 + Lifecycle 通知）。

公共 API 见 docs/02-模块详设/Agent/R2-插件总线与协议.md。
- Port 契约与 DTO：src/hooks/protocols.py、src/hooks/dto.py（保留资产）。
- 共用注册表与可插拔基契约：src/hooks/types.py。
- Lifecycle 通知体系（when）：src/hooks/lifecycle.py。
- AOP 增强体系（how）：src/aop/。
"""
from __future__ import annotations

from .dto import (
    DangerLevel, DisclosurePacket, LLMRequest, LLMResponse, PortCtx, SkillManifest,
    StreamEvent, StreamEventKind, ToolCall, ToolResult, ToolSpec, Usage, make_call,
)
from .lifecycle import (
    PHASE_COUNT,
    LifecycleBus,
    LifecyclePhase,
    Notice,
    runner_notifier,
)
from .protocols import (
    PORT_PROTOCOLS, AuditPort, AuditStore, DisclosureIndex, EvalPort, EventStreamPort,
    HumanPort, LLMPort, MCPPort, MemoryPort, RAGPort, SandboxPort, SkillPort, StorePort,
    TelemetryExporter, TelemetryPort, ToolPort,
)
from .types import (
    Capability, HookBusError, Provider, ProviderRegistry, Registry,
)

__all__ = [
    "LifecycleBus", "LifecyclePhase", "Notice", "PHASE_COUNT", "runner_notifier",
    "Capability", "Provider", "ProviderRegistry", "Registry", "HookBusError",
    "LLMPort", "ToolPort", "SkillPort", "RAGPort", "MemoryPort", "StorePort",
    "SandboxPort", "AuditPort", "AuditStore", "DisclosureIndex", "EvalPort", "TelemetryPort", "TelemetryExporter", "HumanPort",
    "MCPPort", "EventStreamPort", "PORT_PROTOCOLS",
    "LLMRequest", "LLMResponse", "ToolCall", "ToolResult", "ToolSpec", "Usage",
    "DangerLevel", "DisclosurePacket", "SkillManifest", "PortCtx", "make_call", "StreamEvent", "StreamEventKind",
]
