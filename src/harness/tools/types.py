"""R5 工具包基础枚举：来源与沙盒策略的有限取值集合。

本模块只放"取值可枚举"的常量（编码规则 5），供装饰器默认值与各实现共用，
避免魔法字符串散落。工具调用的风险等级另有权威定义（hooks.DangerLevel）。
"""
from __future__ import annotations

from enum import Enum

__all__ = ["ToolSource", "SandboxPolicy"]


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
