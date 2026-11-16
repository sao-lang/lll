"""M3 端口协议族：全部外部能力的接口契约一次定齐（M4~M9 按接口实现）。

运行时只认 Protocol 不认实现；替换实现 = registry.register("llm", MyAdapter())。
P0 只需 llm / tool / telemetry / human 四个有默认实现，其余先冻结契约。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from runtime import DynamicValue

from .dto import LLMRequest, LLMResponse, ToolCall, ToolResult, ToolSpec
from .registry import PortCtx

__all__ = [
    "LLMPort", "ToolPort", "SkillPort", "RAGPort", "MemoryPort", "StorePort",
    "SandboxPort", "AuditPort", "EvalPort", "TelemetryPort", "HumanPort",
    "MCPPort", "PORT_PROTOCOLS",
]


@runtime_checkable
class LLMPort(Protocol):
    """LLM 能力：一次性补全与流式补全。"""

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次性补全；返回含 content / tool_calls / usage 的响应。"""
        ...

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """流式补全；逐片段产出文本。"""
        ...


@runtime_checkable
class ToolPort(Protocol):
    """工具能力：注册、目录查询、调用。"""

    def register(self, spec: ToolSpec, fn: Callable[..., DynamicValue]) -> None:
        """注册工具实现。"""
        ...

    def catalog(self) -> list[ToolSpec]:
        """返回工具目录。"""
        ...

    async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:
        """执行工具调用。"""
        ...


@runtime_checkable
class SkillPort(Protocol):
    """技能能力：渐进式披露正文（part=\"body\" 对应 L3 披露）。"""

    async def body(self, name: str, part: str, ctx: PortCtx) -> str:
        """按名字与分段取技能正文。"""
        ...  # part="body" 渐进式披露


@runtime_checkable
class RAGPort(Protocol):
    """检索增强能力：文档入库与检索（文档形态由实现决定）。"""

    async def ingest(self, documents: list[DynamicValue], ctx: PortCtx) -> int:
        """入库文档，返回成功入库数量。"""
        ...

    async def retrieve(self, query: str, ctx: PortCtx) -> list[DynamicValue]:
        """按查询返回候选片段。"""
        ...


@runtime_checkable
class MemoryPort(Protocol):
    """记忆能力：按命名空间与键读写。"""

    async def read(self, namespace: str, key: str, ctx: PortCtx) -> DynamicValue:
        """读取一条记忆；缺失返回 None。"""
        ...

    async def write(self, namespace: str, key: str, value: DynamicValue, ctx: PortCtx) -> None:
        """写入一条记忆。"""
        ...


@runtime_checkable
class StorePort(Protocol):
    """持久化能力：会话快照存取。"""

    def save(self, session_id: str, snapshot: dict[str, DynamicValue]) -> None:
        """保存会话快照。"""
        ...

    def load(self, session_id: str) -> dict[str, DynamicValue] | None:
        """读取会话快照；不存在返回 None。"""
        ...


@runtime_checkable
class SandboxPort(Protocol):
    """沙盒能力：在隔离环境执行函数（M8 实现）。"""

    async def run(self, fn: Callable[..., DynamicValue], args: DynamicValue, ctx: PortCtx) -> DynamicValue:
        """在沙盒中运行 fn(args)，返回结果。"""
        ...


@runtime_checkable
class AuditPort(Protocol):
    """审计能力：记录结构化事件（M8 实现）。"""

    def record(self, event: dict[str, DynamicValue], ctx: PortCtx) -> None:
        """记录一条审计事件。"""
        ...


@runtime_checkable
class EvalPort(Protocol):
    """评测能力：对输出打分（M9 实现）。"""

    async def score(self, case_id: str, output: DynamicValue, ctx: PortCtx) -> float:
        """按用例 id 对输出评分。"""
        ...


@runtime_checkable
class TelemetryPort(Protocol):
    """可观测能力：span / metric / log 三件套。"""

    def span(self, name: str, ctx: PortCtx) -> AbstractContextManager[DynamicValue]:
        """开启一个追踪跨度（上下文管理器）。"""
        ...

    def metric(self, name: str, value: float, tags: dict[str, DynamicValue] | None = None) -> None:
        """记录一个数值指标。"""
        ...

    def log(self, event: dict[str, DynamicValue]) -> None:
        """记录一条结构化日志。"""
        ...


@runtime_checkable
class HumanPort(Protocol):
    """人工在环能力：向人提问并取回答。"""

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        """提问；options 为候选答案，返回人的回答。"""
        ...


@runtime_checkable
class MCPPort(Protocol):
    """MCP 能力：发现与调用外部工具。"""

    async def list_tools(self, server: str, ctx: PortCtx) -> list[ToolSpec]:
        """列出某服务器提供的工具。"""
        ...

    async def call_tool(self, server: str, tool: str, args: dict[str, DynamicValue], ctx: PortCtx) -> DynamicValue:
        """调用某服务器的工具。"""
        ...


# port 名 → Protocol 类型；供装配期 isinstance 校验适配器是否履约
PORT_PROTOCOLS: dict[str, type] = {
    "llm": LLMPort, "tool": ToolPort, "skill": SkillPort, "rag": RAGPort,
    "memory": MemoryPort, "store": StorePort, "sandbox": SandboxPort,
    "audit": AuditPort, "eval": EvalPort, "telemetry": TelemetryPort,
    "human": HumanPort, "mcp": MCPPort,
}
