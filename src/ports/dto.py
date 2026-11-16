"""M3 跨模块 DTO（pydantic v2）：字段只增不改语义（契约冻结 G0）。

共享信封 Result / Usage / PortError 定义在 runtime/types.py（M1 公共层），
本模块 re-export，保证 M1 不依赖 pydantic 也能独立闭环。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field

from runtime import DynamicValue, Message, PortError, Result, Usage

__all__ = [
    "DangerLevel", "ToolSpec", "ToolCall", "ToolResult", "ToolResultMeta",
    "LLMRequest", "LLMResponse", "Usage", "Result", "PortError", "Message",
]


class ToolResultMeta(TypedDict, total=False):
    """工具调用结果的附加元数据；键由框架在 danger 路由时写入，类型确定。"""

    danger: str  # 工具的危险分级字符串
    source: str  # 来源：local / mcp / skill
    sandbox: bool  # 是否经沙盒执行
    duration_ms: float  # 逻辑时钟记录的耗时
    approved: bool  # CRITICAL 工具是否获批
    retryable: bool  # 错误是否可重试
    extra: DynamicValue  # 预留：适配器自定义的附加数据


def _json_object() -> dict[str, DynamicValue]:
    """pydantic default_factory：为每个实例产出独立的空 JSON 对象字典。"""
    return {}


class DangerLevel(str, Enum):
    """危险分级（ToolSpec 契约的一部分，M4 re-export；M8 按此路由沙盒/审批）。"""

    SAFE = "safe"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


class ToolSpec(BaseModel):
    """工具目录条目（L1 常驻上下文那一行字）。extra=forbid 拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid")

    name: str  # 工具唯一名；MCP 工具用 mcp__<server>__<tool> 全限定名
    description: str = ""  # 一句话说明，进 L1 目录与 prompt
    parameters_schema: dict[str, DynamicValue] = Field(
        default_factory=_json_object, description="JSON Schema；L1 披露时清空以省 token"
    )  # 参数 schema（JSON Schema 结构）
    danger: DangerLevel | None = Field(
        default=None, description="危险分级；注册期必填，None 会被 registry 拒绝"
    )
    source: Literal["local", "mcp", "skill"] = "local"  # 来源：本地 / MCP / 技能


class ToolCall(BaseModel):
    """一次工具调用请求；由 LLM 产出或用户显式构造。extra=forbid。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str  # 调用关联 id，回程 ToolResult 用它对齐
    name: str  # 目标工具名
    args: dict[str, DynamicValue] = Field(
        default_factory=_json_object, description="调用参数；registry 按 args_model 校验"
    )


class ToolResult(BaseModel):
    """工具调用结果信封；业务失败也算正常返回（ok=False 携带 error）。extra=forbid。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str  # 对齐 ToolCall.call_id
    ok: bool  # 调用是否成功
    value: DynamicValue | None = None  # 成功时的返回值，形态由工具决定
    error: str | None = None  # 失败时的错误描述
    meta: ToolResultMeta = Field(
        default_factory=lambda: cast(ToolResultMeta, {}),
        description="附加元数据：danger/source/sandbox/duration_ms 等",
    )


class LLMRequest(BaseModel):
    """LLM 补全请求；Message 是 runtime 的 frozen dataclass，故允许任意类型。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[Message] = Field(default_factory=list)  # 对话历史
    tools_catalog: list[ToolSpec] | None = None  # 可选：注入的工具目录
    model: str | None = None  # 模型名覆盖；None 用适配器默认
    temperature: float | None = None  # 采样温度
    max_tokens: int | None = None  # 最大输出 token


class LLMResponse(BaseModel):
    """LLM 补全响应；usage 直接复用 runtime 的 Usage 信封。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str | None = None  # 文本响应
    tool_calls: list[ToolCall] = Field(default_factory=list)  # 模型请求的工具调用
    usage: Usage = Field(default_factory=Usage)  # token / 成本元数据
    model: str = "unknown"  # 实际服务的模型名
