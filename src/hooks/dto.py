"""R2 DTO：跨模块数据契约（pydantic），全框架统一。

字段只增不改语义；废弃字段先标记 deprecated 至少一个 minor 版本。
所有形态由外部 port 决定的字段统一用 DynamicValue（动态边界具名别名），
禁止散落裸 Any（见 docs/编码规则.md 规则 3）。
每个字段的 Field(description) 说明语义；复杂类在类 docstring 补字段约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from runtime import DynamicValue

__all__ = [
    "DangerLevel",
    "LLMRequest",
    "LLMResponse",
    "PortCtx",
    "StreamEvent",
    "StreamEventKind",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "make_call",
]


class StreamEventKind(str, Enum):
    """结构化流式事件类型（R4 §4.1 v1.2）。

    成员：
        TEXT_DELTA: 文本增量，data 为 str。
        TOOL_CALL_START / DELTA / END: 工具调用生命周期事件。
        HANDOFF: 角色/程序切换，data 为 {"to": ...}。
        USAGE: 末块结算，data 为 Usage。
        DONE: 流结束。
    """

    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    HANDOFF = "handoff"
    USAGE = "usage"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """结构化流式事件（跨 Port/网关统一载体，规则 3 动态边界用具名 kind）。

    字段：
        kind: 事件类型。data: 载荷形态由 kind 决定（文本为 str，工具为 dict，USAGE 为 Usage）。
    """

    kind: StreamEventKind
    data: DynamicValue = None


class _Strict(BaseModel):
    """严格模型基类：拒绝未声明字段（extra=forbid），避免脏数据静默混入。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class DangerLevel:
    """工具风险分级常量：SAFE/LOW/HIGH/CRITICAL。

    属性：
        SAFE/LOW/HIGH/CRITICAL: 四级风险字符串常量。
        ALL: 全部级别的元组，供校验/遍历。
    """

    SAFE = "SAFE"
    LOW = "LOW"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    ALL = (SAFE, LOW, HIGH, CRITICAL)


class Usage(_Strict):
    """token 与成本元数据。

    字段：
        prompt_tokens: 输入 token 数。completion_tokens: 输出 token 数。
        cost_usd: 本步成本（美元）。本地/免费实现可全填 0。
    """

    prompt_tokens: int = Field(default=0, description="输入 token 数")
    completion_tokens: int = Field(default=0, description="输出 token 数")
    cost_usd: float = Field(default=0.0, description="本步成本（美元）")


class PortCtx(_Strict):
    """Port 调用上下文：适配器拿会话信息的唯一途径，不持有会话状态。

    字段：
        session_id: 会话标识。run_id: run 标识。
        trace_span: 链路 span 标识（可选）。
        budget_left: 预算余量快照（R1 Budgets 的只读投影，避免 DTO 反向依赖内核）。
        meta: 临时元数据（动态键，形态由调用方决定）。
    """

    session_id: str = Field(default="", description="会话标识")
    run_id: str = Field(default="", description="run 标识")
    trace_span: str | None = Field(default=None, description="链路 span 标识")
    budget_left: DynamicValue = Field(
        default=None,
        description="预算余量快照（R1 Budgets 的只读投影，避免 DTO 反向依赖内核）",
    )
    meta: dict[str, DynamicValue] = Field(
        default_factory=dict, description="临时元数据"
    )


class ToolCall(_Strict):
    """工具调用。

    字段：
        call_id: 调用关联 id（回填结果时据此匹配）。name: 工具名。
        args: 参数字典（键由工具 schema 决定，故动态）。
        danger: 风险级别（R9 高危路由据此决定是否走沙盒+审批；缺省 SAFE 兼容旧构造）。
        source: 工具来源（local/mcp/skill，审计用）。
    """

    call_id: str = Field(description="调用关联 id")
    name: str = Field(description="工具名")
    args: dict[str, DynamicValue] = Field(default_factory=dict, description="参数字典")
    danger: str = Field(default=DangerLevel.SAFE, description="风险级别 SAFE/LOW/HIGH/CRITICAL")
    source: str = Field(default="local", description="工具来源 local/mcp/skill")


class ToolSpec(_Strict):
    """工具目录条目（L1 披露载体：常驻只有 name+description）。

    字段：
        name: 全局唯一工具名。description: 一句话描述。
        parameters_schema: JSON Schema（L2/L3 才披露）。
        danger: 风险级别（SAFE/LOW/HIGH/CRITICAL）。
        source: 来源（local/mcp/skill）。
    """

    name: str = Field(description="全局唯一工具名")
    description: str = Field(default="", description="一句话描述")
    parameters_schema: dict[str, DynamicValue] = Field(
        default_factory=dict, description="JSON Schema"
    )
    danger: str | None = Field(default=None, description="SAFE/LOW/HIGH/CRITICAL")
    source: str = Field(default="local", description="local/mcp/skill")


class ToolResult(_Strict):
    """工具调用结果信封。

    字段：
        call_id: 调用关联 id。ok: 是否成功返回。
        value: 返回值（形态由工具决定）。error: 错误信息。
        meta: danger/source/sandbox 等元数据（动态键）。
    """

    call_id: str = Field(description="调用关联 id")
    ok: bool = Field(default=True, description="是否成功返回")
    value: DynamicValue = Field(default=None, description="返回值")
    error: str | None = Field(default=None, description="错误信息")
    meta: dict[str, DynamicValue] = Field(
        default_factory=dict, description="danger/source/sandbox 等元数据"
    )


class LLMRequest(_Strict):
    """LLM 补全请求。

    字段：
        messages: 对话消息列表（每项为 dict，形态兼容各家 vendor）。
        tools_catalog: 工具目录（L1 披露；None 表示不披露）。
        model: 模型名覆盖。temperature: 采样温度。
        max_tokens: 最大输出 token。
    """

    messages: list[dict[str, DynamicValue]] = Field(
        default_factory=list, description="对话消息列表"
    )
    tools_catalog: list[ToolSpec] | None = Field(
        default=None, description="工具目录（L1 披露）"
    )
    model: str | None = Field(default=None, description="模型名覆盖")
    temperature: float | None = Field(default=None, description="采样温度")
    max_tokens: int | None = Field(default=None, description="最大输出 token")
    task_hint: str | None = Field(default=None, description="任务类型提示（如 code/chat），供 R4 路由选模型")


class LLMResponse(_Strict):
    """LLM 补全响应。

    字段：
        content: 文本正文。tool_calls: 工具调用列表。
        usage: token 与成本。model: 实际使用的模型。
    """

    content: str | None = Field(default=None, description="文本正文")
    tool_calls: list[ToolCall] = Field(default_factory=list, description="工具调用列表")
    usage: Usage = Field(default_factory=Usage, description="token 与成本")
    model: str = Field(default="", description="实际使用的模型")
    error: str | None = Field(default=None, description="后端错误码/消息；非 None 表示该次调用失败（R3 据此重试/熔断）")


def make_call(
    name: str,
    args: dict[str, DynamicValue],
    call_id: str = "",
    *,
    danger: str = DangerLevel.SAFE,
    source: str = "local",
) -> ToolCall:
    """构造 ToolCall 的便捷函数。

    参数：
        name: 工具名。args: 参数字典。
        call_id: 关联 id；留空时用 name 兜底（保证 id 非空）。
        danger: 风险级别（高危路由用）。source: 工具来源。
    返回：
        新建的 ToolCall。
    """
    return ToolCall(call_id=call_id or name, name=name, args=args, danger=danger, source=source)
