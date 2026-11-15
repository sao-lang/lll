"""M4 自带 fake（CL-2）：沙盒 / 审批 / MCP server / 技能正文，全部可离线跑通。

M8 后续只需用真实实现替换这些 Port，工具系统代码零改动。
"""
from __future__ import annotations

import inspect
from typing import Any

from agentkit.ports import DangerLevel, PortCtx, ToolSpec

__all__ = ["FakeSandbox", "StaticApprover", "FakeMCPTransport", "DictSkillProvider"]


class FakeSandbox:
    """SandboxPort 测试替身：记录调用并执行工具（真实沙盒由 M8 提供）。"""

    name = "fake-sandbox"

    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, fn: Any, args: Any, ctx: PortCtx) -> Any:
        self.runs.append(getattr(args, "__class__", type(args)).__name__)
        out = fn()
        return await out if inspect.isawaitable(out) else out


class StaticApprover:
    name = "static-approver"

    def __init__(self, approve: bool = True) -> None:
        self.approve_flag = approve
        self.questions: list[str] = []

    async def approve(self, question: str, ctx: PortCtx) -> bool:
        self.questions.append(question)
        return self.approve_flag

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        self.questions.append(question)
        return "approve" if self.approve_flag else "deny"


class FakeMCPTransport:
    """一个假 MCP server：两个只读工具，供注册/披露/调用链路联测。"""

    name = "fake-mcp-transport"

    def __init__(self, server: str = "demo") -> None:
        self.server = server

    async def list_tools(self, server: str) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="add", description="两数相加",
                parameters_schema={"type": "object", "properties": {
                    "a": {"type": "integer"}, "b": {"type": "integer"}},
                },
                danger=DangerLevel.SAFE,
            ),
            ToolSpec(
                name="weather", description="查询天气",
                parameters_schema={"type": "object", "properties": {"city": {"type": "string"}}},
                danger=DangerLevel.SAFE,
            ),
        ]

    async def call_tool(self, server: str, tool: str, args: dict[str, Any], ctx: PortCtx) -> Any:
        if tool == "add":
            return args["a"] + args["b"]
        if tool == "weather":
            return f"{args['city']}: 晴 22°C"
        raise ValueError(f"unknown mcp tool {tool}")

    async def ping(self, server: str) -> bool:
        return True


class DictSkillProvider:
    name = "dict-skill-provider"

    def __init__(self, bodies: dict[str, str] | None = None) -> None:
        self.bodies = bodies or {"python-expert": "你是一名 Python 专家，请给出可运行代码。"}

    def body(self, name: str) -> str:
        return self.bodies.get(name, "")
