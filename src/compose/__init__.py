"""组合层 = 算子核心：A 类算子装配 Program + B/C 类构造注入 R1 的 call。

框架真正核心。依赖方向：工具层(R3~R9) → 组合层(本包) → 内核 R1。
R1 一行不改，只解释 Program；本包构造 call 注入 Runner(call=...)。

公共 API 见 docs/02-模块详设/Agent/R2-算子与装配.md 与 R12-内置架构与组合.md。
"""
from __future__ import annotations

from .context import (
    CallContext, FaultRecord, RoutingTarget, Invocation, Next,
    FAULT_KEY, ROUTING_TARGET_KEY,
)
from .failure import FailureClass
from .call import (
    CallFn,
    build_call,
    danger_route,
    injection_guard,
    retry_call,
    timeout,
)
from .dispatch import dispatch, make_dispatch
from .tool_layer import layer_program
from .agent import Agent
from .guards import (
    CallState,
    FaultRecord,
    RoutingTarget,
)
from .input import bind_input, current_input
from .program import (
    branch,
    fallback,
    gate,
    loop,
    map_effect,
    map_state,
    par,
    race,
    retry,
    seq,
)
# 原 R2 hooks 资产归位（规格 P2）：DTO / Port 协议 / Registry / notify 相位。
from .dto import (
    DangerLevel, DisclosurePacket, LLMRequest, LLMResponse, PortCtx, SkillManifest,
    StreamEvent, StreamEventKind, ToolCall, ToolResult, ToolSpec, Usage, make_call,
)
from .registry import (
    Capability, HookBusError, Provider, ProviderRegistry, Registry,
)
from .ports import (
    PORT_PROTOCOLS, AuditPort, AuditStore, DisclosureIndex, EvalPort, EventStreamPort,
    HumanPort, LLMPort, MCPPort, MemoryPort, RAGPort, SandboxPort, SkillPort, StorePort,
    TelemetryExporter, TelemetryPort, ToolPort,
)
from .notify import (
    PHASE_COUNT, ErrorHook, LifecycleBus, LifecyclePhase, Notice, runner_notifier,
)

__all__ = [
    # ①A 类算子（Program → Program）
    "seq", "par", "branch", "race", "loop", "map_state", "map_effect",
    "fallback", "gate", "retry",
    "current_input", "bind_input",
    # call 装饰器 / 装配
    "CallFn", "build_call",
    "retry_call", "timeout", "danger_route", "injection_guard",
    # dispatch
    "dispatch", "make_dispatch",
    # ToolLayer 外壳（P4-P8）
    "layer_program",
    # R12 用户入口装配器
    "Agent",
    # 协作 DTO（领域语义，无编排角色）
    "CallContext", "FaultRecord", "RoutingTarget", "Invocation", "Next",
    "FAULT_KEY", "ROUTING_TARGET_KEY",
    "FailureClass", "CallState",
    # 原 R2 hooks 资产归位（规格 P2）
    "DangerLevel", "DisclosurePacket", "LLMRequest", "LLMResponse", "PortCtx",
    "SkillManifest", "StreamEvent", "StreamEventKind", "ToolCall", "ToolResult",
    "ToolSpec", "Usage", "make_call",
    "Capability", "HookBusError", "Provider", "ProviderRegistry", "Registry",
    "PORT_PROTOCOLS", "AuditPort", "AuditStore", "DisclosureIndex", "EvalPort",
    "EventStreamPort", "HumanPort", "LLMPort", "MCPPort", "MemoryPort", "RAGPort",
    "SandboxPort", "SkillPort", "StorePort", "TelemetryExporter", "TelemetryPort", "ToolPort",
    "PHASE_COUNT", "ErrorHook", "LifecycleBus", "LifecyclePhase", "Notice", "runner_notifier",
]
