"""组合层 = 算子核心：A 类算子装配 Program + B/C 类构造注入 R1 的 call。

框架真正核心。依赖方向：工具层(R3~R9) → 组合层(本包) → 内核 R1。
R1 一行不改，只解释 Program；本包构造 call 注入 Runner(call=...)。

公共 API 见 docs/02-模块详设/Agent/R2-算子与装配.md 与 R12-内置架构与组合.md。
"""
from __future__ import annotations

from .call_chain import CompositeCall
from .adapters import WardenGuardAdapter, envelope_decorator
from .context import (
    CallContext, FaultRecord, RoutingTarget, Verdict, Invocation, Next,
    FAULT_KEY, ROUTING_TARGET_KEY,
)
from .chain_roles import (
    Prelude, Warden, Envelope, Refiner, Salvage, FailureClass, Role, ROLE_NAMES,
)
# AOPContainer 是组合层 CompositeCall 的迁移别名（aop 取消，规格 D-3/P2）
AOPContainer = CompositeCall
from .call import (
    CallFn,
    Guard,
    Verdict,
    build_call,
    danger_route,
    injection_guard,
    retry_call,
    timeout,
    with_guard,
)
from .dispatch import dispatch, make_dispatch
from .tool_layer import layer_program
from .guards import (
    CallState,
    DegradationGuard,
    FaultRecord,
    RoutingTarget,
    degrade_retry,
)
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
    # 插件 → 组合层原语适配器
    "WardenGuardAdapter", "envelope_decorator",
    # ①A 类算子（Program → Program）
    "seq", "par", "branch", "race", "loop", "map_state", "map_effect",
    "fallback", "gate", "retry",
    # ②守卫 / ③call 装饰器 / 装配
    "Guard", "Verdict", "CallFn", "with_guard", "build_call",
    "retry_call", "timeout", "danger_route", "injection_guard",
    # dispatch
    "dispatch", "make_dispatch",
    # ToolLayer 外壳（P4-P8）
    "layer_program",
    # call 装配器（承接 AOPContainer 编排能力）
    "CompositeCall", "AOPContainer",
    # 协作 DTO + 内部编排槽位协议（原 aop 归位）
    "CallContext", "FaultRecord", "RoutingTarget", "Verdict", "Invocation", "Next",
    "FAULT_KEY", "ROUTING_TARGET_KEY",
    "Prelude", "Warden", "Envelope", "Refiner", "Salvage", "FailureClass", "Role", "ROLE_NAMES",
    # R3 拆分：②降级守卫 + 具名 DTO
    "FaultRecord", "RoutingTarget", "CallState", "DegradationGuard", "degrade_retry",
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
