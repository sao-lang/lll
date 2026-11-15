"""M3 跨模块 DTO（pydantic v2）：字段只增不改语义（契约冻结 G0）。

共享信封 Result / Usage / PortError 定义在 runtime/types.py（M1 公共层），
本模块 re-export，保证 M1 不依赖 pydantic 也能独立闭环。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.runtime import Message, PortError, Result, Usage

__all__ = [
    "DangerLevel", "ToolSpec", "ToolCall", "ToolResult", "LLMRequest",
    "LLMResponse", "Usage", "Result", "PortError", "Message",
]


class DangerLevel(str, Enum):
    """危险分级（ToolSpec 契约的一部分，M4 re-export；M8 按此路由沙盒/审批）。"""

    SAFE = "safe"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


class ToolSpec(BaseModel):
    """工具目录条目（L1 常驻上下文那一行字）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    danger: DangerLevel | None = None
    source: Literal["local", "mcp", "skill"] = "local"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    ok: bool
    value: Any | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class LLMRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[Message] = Field(default_factory=list)
    tools_catalog: list[ToolSpec] | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = "unknown"
