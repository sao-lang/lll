"""R2 Port 协议族：全部外部能力的接口契约，一次定齐。

运行时只认 Protocol 不认实现；替换实现 = registry.register(name, adapter)。
所有适配器公共契约：无会话状态（ctx 传递）、异常折叠为 Result、带 name。

协议清单（13 个 Port）：
    LLMPort 模型补全 / ToolPort 工具 / SkillPort 技能 / RAGPort 检索 /
    MemoryPort 记忆 / StorePort 持久化 / SandboxPort 沙盒 / AuditPort 审计 /
    EvalPort 评测 / TelemetryPort 可观测 / HumanPort 人工在环 /
    MCPPort MCP 工具 / EventStreamPort 事件流（L3 接入契约）。

约定：每个 Protocol 方法签名即契约（参数 + 返回值全部标注）；
方法体统一 `...` 占位，具体实现由注入的适配器提供。
公共 API 见 docs/02-模块详设/Agent/R2-插件总线与协议.md §4。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from runtime import DynamicValue

from .dto import LLMRequest, LLMResponse, PortCtx, ToolCall, ToolResult, ToolSpec

__all__ = [
    "PORT_PROTOCOLS",
    "AuditPort",
    "EvalPort",
    "EventStreamPort",
    "HumanPort",
    "LLMPort",
    "MCPPort",
    "MemoryPort",
    "RAGPort",
    "SandboxPort",
    "SkillPort",
    "StorePort",
    "TelemetryPort",
    "ToolPort",
]


@runtime_checkable
class LLMPort(Protocol):
    """LLM 能力：一次性补全与流式补全。"""
    name: str

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次性补全：返回结构化响应（含 tool_calls 与 usage）。"""
        ...

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """流式补全：逐块产出文本，取消由调用方传播。"""
        ...


@runtime_checkable
class ToolPort(Protocol):
    """工具能力：注册、目录查询、调用。"""
    name: str

    def register(self, spec: ToolSpec, fn: Callable[..., DynamicValue]) -> None:
        """注册工具：spec 定义 schema，fn 是实际实现。"""
        ...

    def catalog(self) -> list[ToolSpec]:
        """返回工具目录（渐进披露由 DisclosureIndex 投影）。"""
        ...

    async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:
        """调用工具：异常折叠为 ToolResult，不抛穿。"""
        ...


@runtime_checkable
class SkillPort(Protocol):
    """技能能力：渐进式披露正文。"""
    name: str

    def discover(self) -> list[DynamicValue]:
        """发现所有可用技能清单。"""
        ...

    def catalog(self) -> list[DynamicValue]:
        """返回技能目录（name/version/触发词，常驻 L1）。"""
        ...

    def body(self, name: str) -> str:
        """返回技能正文（L3 按需加载），正文须过注入检测。"""
        ...


@runtime_checkable
class RAGPort(Protocol):
    """检索增强能力：入库与检索。"""
    name: str

    async def ingest(self, documents: list[DynamicValue], ctx: PortCtx) -> int:
        """入库文档，返回成功入库的块数。"""
        ...

    async def retrieve(self, query: str, ctx: PortCtx) -> list[DynamicValue]:
        """检索命中列表，每项带引用溯源信息。"""
        ...


@runtime_checkable
class MemoryPort(Protocol):
    """记忆能力：按命名空间读写。"""
    name: str

    async def read(self, namespace: str, key: str, ctx: PortCtx) -> DynamicValue:
        """按命名空间与键读取记忆；不存在返回 None。"""
        ...

    async def write(self, namespace: str, key: str, value: DynamicValue, ctx: PortCtx) -> None:
        """写入记忆；内容须过注入检测且不含 secret 明文。"""
        ...

    async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:
        """遗忘指定记忆（TTL 或显式删除）。"""
        ...


@runtime_checkable
class StorePort(Protocol):
    """持久化能力：会话快照存取。"""
    name: str

    def save(self, session_id: str, snapshot: dict[str, DynamicValue]) -> None:
        """保存会话快照；结构须可确定性 JSON 序列化。"""
        ...

    def load(self, session_id: str) -> dict[str, DynamicValue] | None:
        """读取会话快照；不存在返回 None。"""
        ...


@runtime_checkable
class SandboxPort(Protocol):
    """沙盒能力：在隔离环境执行函数。"""
    name: str

    async def run(
        self, fn: Callable[..., DynamicValue], args: DynamicValue, ctx: PortCtx,
    ) -> DynamicValue:
        """在隔离环境执行函数，资源限额与网络白名单由实现保证。"""
        ...


@runtime_checkable
class AuditPort(Protocol):
    """审计能力：记录结构化事件。"""
    name: str

    def record(self, event: dict[str, DynamicValue], ctx: PortCtx) -> None:
        """追加审计事件（append-only + 哈希链，不可旁路）。"""
        ...


@runtime_checkable
class EvalPort(Protocol):
    """评测能力：对输出打分。"""
    name: str

    async def score(self, case_id: str, output: DynamicValue, ctx: PortCtx) -> float:
        """对单个评测用例的输出打分（0~1）。"""
        ...


@runtime_checkable
class TelemetryPort(Protocol):
    """可观测能力：span / metric / log 三件套。"""
    name: str

    def span(self, name: str, ctx: PortCtx) -> AbstractContextManager[DynamicValue]:
        """开启链路 span，返回上下文管理器。"""
        ...

    def metric(self, name: str, value: float, tags: dict[str, DynamicValue] | None = None) -> None:
        """记录指标，tags 为可选维度标签。"""
        ...

    def log(self, event: dict[str, DynamicValue]) -> None:
        """记录结构化日志事件。"""
        ...


@runtime_checkable
class HumanPort(Protocol):
    """人工在环能力：向人提问并取回答。"""
    name: str

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        """向人提问并返回回答；挂起态须能跨周期恢复。"""
        ...


@runtime_checkable
class MCPPort(Protocol):
    """MCP 能力：发现与调用外部工具。"""
    name: str

    async def list_tools(self, server: str, ctx: PortCtx) -> list[ToolSpec]:
        """列出 MCP server 的工具；L1 披露只进目录不进 schema。"""
        ...

    async def call_tool(
        self, server: str, tool: str, args: dict[str, DynamicValue], ctx: PortCtx,
    ) -> DynamicValue:
        """调用 MCP 工具，返回形态由工具决定。"""
        ...


@runtime_checkable
class EventStreamPort(Protocol):
    """事件流能力（L3 接入契约）：只读订阅标准事件流。"""
    name: str

    def subscribe(self, topic: str, since: int | None = None) -> AsyncIterator[DynamicValue]:
        """订阅事件流，从 since 序号续读；只读不侵入内核。"""
        ...

    def topics(self) -> list[str]:
        """返回可订阅的 topic 列表。"""
        ...


# port 名 → Protocol 类型；供装配期 isinstance 校验适配器是否履约。
# 键是 R1 EffectKind / 装配约定的 port 名，值是对应的 R2 Protocol 类。
# 用途：registry.register("llm", adapter) 后，可用本表 isinstance(adapter, PORT_PROTOCOLS["llm"])
# 判断适配器是否实现了该 port 要求的全部方法（缺一即装配期失败）。
PORT_PROTOCOLS: dict[str, type] = {
    "llm": LLMPort, "tool": ToolPort, "skill": SkillPort, "rag": RAGPort,
    "memory": MemoryPort, "store": StorePort, "sandbox": SandboxPort,
    "audit": AuditPort, "eval": EvalPort, "telemetry": TelemetryPort,
    "human": HumanPort, "mcp": MCPPort, "event_stream": EventStreamPort,
}
