"""R5 工具包基础枚举：来源、沙盒策略与风险等级的有限取值集合（规格 P5）。

本模块只放"取值可枚举"的常量（编码规则 5），供装饰器默认值与各实现共用，
避免魔法字符串散落。DangerLevel/coerce 语义归属 R5（⑤Port 契约）；本模块作为
R5 内部统一消费门面，从 hooks.dto 重导出（跨层 DTO 字段的物理约束使定义须留在
hooks 底层，避免 hooks 反向依赖 harness 成环）。
"""
from __future__ import annotations

from enum import Enum

# R5 门面：DangerLevel 权威定义在 hooks.dto（跨层 DTO 字段的物理约束），
# 此处重导出作为 R5 内部统一消费入口（规格 P5：R5 是 danger 语义归属层）。
from compose import DangerLevel

__all__ = ["ToolSource", "SandboxPolicy", "DangerLevel"]


class ToolSource(str, Enum):
    """工具来源（规则 5：有限取值集合用 str, Enum，与 JSON Schema 字符串互通）。

    成员：
        LOCAL: 本地 @tool 装饰函数。MCP: 外部 MCP server 注入。SKILL: 技能暴露的工具。
    """

    LOCAL = "local"
    MCP = "mcp"
    SKILL = "skill"


class SandboxPolicy(str, Enum):
    """沙盒策略（@tool 装饰器默认值；运行期路由以 danger 为准，本枚举仅作声明）。

    成员：
        AUTO: 由 danger 决定。ALWAYS: 总是沙盒。NEVER: 从不沙盒。
    """

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"
