"""R2 插件总线与协议（ARCH-v5）：tapable 式扩展点 + Host + 全部 Port 契约。

公共 API 见 docs/02-模块详设/Agent/R2-插件总线与协议.md §4。
Hook 调用语义（Bail/Waterfall/Loop/Parallel/Around）见 hooks/v5.py。
"""
from __future__ import annotations

from .dto import (
    DangerLevel, LLMRequest, LLMResponse, PortCtx, StreamEvent, StreamEventKind,
    ToolCall, ToolResult, ToolSpec, Usage, make_call,
)
from .protocols import (
    PORT_PROTOCOLS, AuditPort, AuditStore, EvalPort, EventStreamPort, HumanPort, LLMPort,
    MCPPort, MemoryPort, RAGPort, SandboxPort, SkillPort, StorePort, TelemetryExporter, TelemetryPort, ToolPort,
)
from .types import (
    Capability, Decision, DecisionAction, HookBusError, HookCtx,
    LifecycleEvent, Matcher, Provider, Registry, RetrySpec,
)
from .v5 import (
    BailHook, AroundHook, HookKind, Host, LoopHook, ParallelHook, Plugin,
    RequestScope, Signal, SignalKind, WaterfallHook,
)

__all__ = [
    "HookKind", "Host", "RequestScope", "Signal", "SignalKind", "Plugin",
    "BailHook", "WaterfallHook", "ParallelHook", "LoopHook", "AroundHook",
    "HookCtx", "Decision", "DecisionAction", "RetrySpec", "Matcher",
    "Capability", "Provider", "Registry", "HookBusError", "LifecycleEvent",
    "LLMPort", "ToolPort", "SkillPort", "RAGPort", "MemoryPort", "StorePort",
    "SandboxPort", "AuditPort", "AuditStore", "EvalPort", "TelemetryPort", "TelemetryExporter", "HumanPort",
    "MCPPort", "EventStreamPort", "PORT_PROTOCOLS",
    "LLMRequest", "LLMResponse", "ToolCall", "ToolResult", "ToolSpec", "Usage",
    "DangerLevel", "PortCtx", "make_call", "StreamEvent", "StreamEventKind",
]
