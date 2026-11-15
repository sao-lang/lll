"""M3 端口协议层：契约 + DTO + 注册表 + call 折叠 + 默认适配器。"""
from .call import KIND_TO_PORT, call, classify_exception, make_call
from .dto import (
    DangerLevel, LLMRequest, LLMResponse, ToolCall, ToolResult, ToolSpec,
)
from .protocols import (
    PORT_PROTOCOLS, AuditPort, EvalPort, HumanPort, LLMPort, MCPPort,
    MemoryPort, RAGPort, SandboxPort, SkillPort, StorePort, TelemetryPort, ToolPort,
)
from .registry import BUILTIN_PORTS, PortContractError, PortCtx, PortRegistry, port_ctx_from

__all__ = [
    "DangerLevel", "ToolSpec", "ToolCall", "ToolResult", "LLMRequest", "LLMResponse",
    "PortRegistry", "PortCtx", "PortContractError", "BUILTIN_PORTS", "port_ctx_from",
    "call", "make_call", "classify_exception", "KIND_TO_PORT",
    "LLMPort", "ToolPort", "SkillPort", "RAGPort", "MemoryPort", "StorePort",
    "SandboxPort", "AuditPort", "EvalPort", "TelemetryPort", "HumanPort",
    "MCPPort", "PORT_PROTOCOLS",
]
