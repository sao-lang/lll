"""R2 插件总线与协议：唯一扩展点 Hook + 统一返回 Decision + 全部 Port 契约。

公共 API 见 docs/02-模块详设/Agent/R2-插件总线与协议.md §4。
"""
from __future__ import annotations

from .bus import DispatchResult, HookBus
from .dto import (
    DangerLevel, LLMRequest, LLMResponse, PortCtx, ToolCall, ToolResult, ToolSpec, Usage, make_call,
)
from .protocols import (
    PORT_PROTOCOLS, AuditPort, EvalPort, EventStreamPort, HumanPort, LLMPort,
    MCPPort, MemoryPort, RAGPort, SandboxPort, SkillPort, StorePort, TelemetryPort, ToolPort,
)
from .types import (
    Capability, Decision, DecisionAction, Hook, HookBusError, HookCtx, HookMode,
    LifecycleEvent, Matcher, Provider, Registry, RetrySpec,
)

__all__ = [
    "HookBus", "DispatchResult", "Hook", "HookCtx", "HookMode", "Decision",
    "DecisionAction", "RetrySpec", "Matcher", "Capability", "Provider", "Registry",
    "HookBusError", "LifecycleEvent",
    "LLMPort", "ToolPort", "SkillPort", "RAGPort", "MemoryPort", "StorePort",
    "SandboxPort", "AuditPort", "EvalPort", "TelemetryPort", "HumanPort",
    "MCPPort", "EventStreamPort", "PORT_PROTOCOLS",
    "LLMRequest", "LLMResponse", "ToolCall", "ToolResult", "ToolSpec", "Usage",
    "DangerLevel", "PortCtx", "make_call",
]
