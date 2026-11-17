"""R2 DTO：跨模块数据契约（pydantic），全框架统一。

字段只增不改语义；废弃字段先标记 deprecated 至少一个 minor 版本。
所有形态由外部 port 决定的字段统一用 DynamicValue（动态边界具名别名），
禁止散落裸 Any（见 docs/编码规则.md 规则 4）。
"""
from __future__ import annotations

from runtime import DynamicValue

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LLMRequest", "LLMResponse", "ToolCall", "ToolResult", "ToolSpec",
    "Usage", "DangerLevel", "PortCtx", "make_call",
]


class _Strict(BaseModel):
    """严格模型基类：拒绝未声明字段。"""
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class DangerLevel:
    """工具风险分级：SAFE/LOW/HIGH/CRITICAL。"""
    SAFE = "SAFE"
    LOW = "LOW"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    ALL = (SAFE, LOW, HIGH, CRITICAL)


class Usage(_Strict):
    """token 与成本元数据。"""
    prompt_tokens: int = Field(default=0, description="输入 token 数")
    completion_tokens: int = Field(default=0, description="输出 token 数")
    cost_usd: float = Field(default=0.0, description="本步成本（美元）")


class PortCtx(_Strict):
    """Port 调用上下文：适配器拿会话信息的唯一途径，不持有会话状态。"""
    session_id: str = Field(default="", description="会话标识")
    run_id: str = Field(default="", description="run 标识")
    trace_span: str | None = Field(default=None, description="链路 span 标识")
    budget_left: DynamicValue = Field(default=None, description="预算余量快照（R1 Budgets 的只读投影，避免 DTO 反向依赖内核）")
    meta: dict[str, DynamicValue] = Field(default_factory=dict, description="临时元数据")


class ToolCall(_Strict):
    """工具调用。"""
    call_id: str = Field(description="调用关联 id")
    name: str = Field(description="工具名")
    args: dict[str, DynamicValue] = Field(default_factory=dict, description="参数字典")


class ToolSpec(_Strict):
    """工具目录条目（L1 披露载体）。"""
    name: str = Field(description="全局唯一工具名")
    description: str = Field(default="", description="一句话描述")
    parameters_schema: dict[str, DynamicValue] = Field(default_factory=dict, description="JSON Schema")
    danger: str | None = Field(default=None, description="SAFE/LOW/HIGH/CRITICAL")
    source: str = Field(default="local", description="local/mcp/skill")


class ToolResult(_Strict):
    """工具调用结果信封。"""
    call_id: str = Field(description="调用关联 id")
    ok: bool = Field(default=True, description="是否成功返回")
    value: DynamicValue = Field(default=None, description="返回值")
    error: str | None = Field(default=None, description="错误信息")
    meta: dict[str, DynamicValue] = Field(default_factory=dict, description="danger/source/sandbox 等元数据")


class LLMRequest(_Strict):
    """LLM 补全请求。"""
    messages: list[dict[str, DynamicValue]] = Field(default_factory=list, description="对话消息列表")
    tools_catalog: list[ToolSpec] | None = Field(default=None, description="工具目录（L1 披露）")
    model: str | None = Field(default=None, description="模型名覆盖")
    temperature: float | None = Field(default=None, description="采样温度")
    max_tokens: int | None = Field(default=None, description="最大输出 token")


class LLMResponse(_Strict):
    """LLM 补全响应。"""
    content: str | None = Field(default=None, description="文本正文")
    tool_calls: list[ToolCall] = Field(default_factory=list, description="工具调用列表")
    usage: Usage = Field(default_factory=Usage, description="token 与成本")
    model: str = Field(default="", description="实际使用的模型")


def make_call(name: str, args: dict[str, DynamicValue], call_id: str = "") -> ToolCall:
    """构造 ToolCall 的便捷函数。"""
    return ToolCall(call_id=call_id or name, name=name, args=args)
