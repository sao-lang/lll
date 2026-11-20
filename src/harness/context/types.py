"""R7 上下文工程核心 DTO：上下文段、裁剪策略与压缩阈值常量。

本模块只描述数据结构与具名常量，不含组装/压缩逻辑（规则 1：魔法值具名）。
TokenBudget 复用 R6 ``harness.rag.types.TokenBudget``，不在此重定义。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "TrimPolicy",
    "ContextSection",
    "TRIGGER_RATIO",
    "DEFAULT_TRIGGER_TURNS",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_PIN_PATTERNS",
]


class TrimPolicy(StrEnum):
    """段落裁剪策略（有限取值集合，禁止裸魔法串）。

    成员：
        PIN: 钉住不裁（system / 高价值段）。
        SUMMARY: 摘要后保留。
        HEAD: 保留头部。
        TAIL: 保留尾部。
    """

    PIN = "pin"
    SUMMARY = "summary"
    HEAD = "head"
    TAIL = "tail"


@dataclass(frozen=True, slots=True)
class ContextSection:
    """一段待组装的上下文。

    字段：
        name: 段名（system 段名由 ``SYSTEM_SECTION_NAME`` 约定，不可被覆盖）。
        content: 段正文。
        quota: token 配额上限。
        priority: 优先级（小者先排，system 段最小）。
        trim_policy: 超配额时的裁剪策略。
    """

    name: str
    content: str
    quota: int
    priority: int
    trim_policy: TrimPolicy


# 压缩阈值（设计 §5：窗口 70% / 12 轮 / 保留最近 4 轮）。
TRIGGER_RATIO: float = 0.7
DEFAULT_TRIGGER_TURNS: int = 12
DEFAULT_KEEP_RECENT: int = 4
# 高价值钉住特征：system 段、未完成事项标记（保留用户任务线索）。
DEFAULT_PIN_PATTERNS: tuple[str, ...] = ("system", "待办", "未完成")
