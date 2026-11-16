"""M3 端口协议族：全部外部能力的接口契约一次定齐（M4~M9 按接口实现）。

运行时只认 Protocol 不认实现；替换实现 = registry.register("llm", MyAdapter())。
P0 只需 llm / tool / telemetry / human 四个有默认实现，其余先冻结契约。
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, AsyncIterator, Callable, Protocol, runtime_checkable

from .dto import LLMRequest, LLMResponse, ToolCall, ToolResult, ToolSpec
from .registry import PortCtx

__all__ = [
    "LLMPort", "ToolPort", "SkillPort", "RAGPort", "MemoryPort", "StorePort",
    "SandboxPort", "AuditPort", "EvalPort", "TelemetryPort", "HumanPort",
    "MCPPort", "PORT_PROTOCOLS",
]


@runtime_checkable
class LLMPort(Protocol):
    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse: ...
    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]: ...


@runtime_checkable
class ToolPort(Protocol):
    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> None: ...
    def catalog(self) -> list[ToolSpec]: ...
    async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult: ...


@runtime_checkable
class SkillPort(Protocol):
    async def body(self, name: str, part: str, ctx: PortCtx) -> str: ...  # part="body" 渐进式披露


@runtime_checkable
class RAGPort(Protocol):
    async def ingest(self, documents: list[Any], ctx: PortCtx) -> int: ...
    async def retrieve(self, query: str, ctx: PortCtx) -> list[Any]: ...


@runtime_checkable
class MemoryPort(Protocol):
    async def read(self, namespace: str, key: str, ctx: PortCtx) -> Any: ...
    async def write(self, namespace: str, key: str, value: Any, ctx: PortCtx) -> None: ...


@runtime_checkable
class StorePort(Protocol):
    def save(self, session_id: str, snapshot: dict[str, Any]) -> None: ...
    def load(self, session_id: str) -> dict[str, Any] | None: ...


@runtime_checkable
class SandboxPort(Protocol):
    async def run(self, fn: Callable[..., Any], args: Any, ctx: PortCtx) -> Any: ...


@runtime_checkable
class AuditPort(Protocol):
    def record(self, event: dict[str, Any], ctx: PortCtx) -> None: ...


@runtime_checkable
class EvalPort(Protocol):
    async def score(self, case_id: str, output: Any, ctx: PortCtx) -> float: ...


@runtime_checkable
class TelemetryPort(Protocol):
    def span(self, name: str, ctx: PortCtx) -> AbstractContextManager[Any]: ...
    def metric(self, name: str, value: float, tags: dict[str, Any] | None = None) -> None: ...
    def log(self, event: dict[str, Any]) -> None: ...


@runtime_checkable
class HumanPort(Protocol):
    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str: ...


@runtime_checkable
class MCPPort(Protocol):
    async def list_tools(self, server: str, ctx: PortCtx) -> list[ToolSpec]: ...
    async def call_tool(self, server: str, tool: str, args: dict[str, Any], ctx: PortCtx) -> Any: ...


PORT_PROTOCOLS: dict[str, type] = {
    "llm": LLMPort, "tool": ToolPort, "skill": SkillPort, "rag": RAGPort,
    "memory": MemoryPort, "store": StorePort, "sandbox": SandboxPort,
    "audit": AuditPort, "eval": EvalPort, "telemetry": TelemetryPort,
    "human": HumanPort, "mcp": MCPPort,
}
