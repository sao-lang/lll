"""R12 内置架构：10 种 A 类算子预设配方（规格 P6）。

对外 API 不变（R12 §4 构造签名）；每个架构 compile() -> Program，R1 只认 Program。
"""
from __future__ import annotations

from .base import Arch, JoinStrategy, MajorityVote, StaticCheckIssue, StaticCheckReport
from .recipes import (
    Blackboard, Graph, Handoff, PlanExecute, ReAct, Reflection,
    Router, RouterSkill, Supervisor, ToolUse,
)

__all__ = [
    "Arch", "JoinStrategy", "MajorityVote", "StaticCheckIssue", "StaticCheckReport",
    "ToolUse", "ReAct", "PlanExecute", "Reflection", "Router",
    "RouterSkill", "Supervisor", "Handoff", "Blackboard", "Graph",
]
