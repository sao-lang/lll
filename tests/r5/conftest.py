"""R5 测试夹具：R9 未实现，用 fake SandboxPort/HumanPort/AuditPort 补齐依赖。

闭环纪律：R5 路由强依赖 R9 三 Port，但 R9 代码尚未开发；本模块自带 fake
（CL-2），使 danger 四路路由可独立测试，不依赖 R9 实现。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import compose
from compose import PortCtx, ToolSpec
from harness.tools import MCPTransport, clear_pending


class FakeSandbox:
    """fake 沙盒：记录是否被调用，返回注入的执行结果。"""

    name = "fake_sandbox"
    calls: list[str]

    def __init__(self, result: Any = "sandbox-ok") -> None:
        self.result = result
        self.calls = []

    async def run(self, fn: Callable[..., Any], args: Any, ctx: PortCtx) -> Any:
        self.calls.append("run")
        out = fn(**(args if isinstance(args, dict) else {}))
        if hasattr(out, "__await__"):
            out = await out
        return out if self.result is None else self.result


class FakeHuman:
    """fake 人工审批：记录提问，返回预设答复。"""

    name = "fake_human"
    questions: list[str]

    def __init__(self, answer: str = "approve") -> None:
        self.answer = answer
        self.questions = []

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        self.questions.append(question)
        return self.answer


class FakeAudit:
    """fake 审计：记录每次审计事件。"""

    name = "fake_audit"
    events: list[dict[str, Any]]

    def __init__(self) -> None:
        self.events = []

    async def record(self, event: dict[str, Any], ctx: PortCtx) -> None:
        self.events.append(event)


class FakeInjectionDetector:
    """fake 注入检测器：命中预设关键词返回 True。"""

    name = "fake_detector"

    def __init__(self, *keywords: str) -> None:
        self.keywords = keywords

    def detect(self, text: str) -> bool:
        return any(k in text for k in self.keywords)


class FakeFetcher:
    """fake 网页抓取器：返回预设正文，记录 URL（R5-T09 用）。"""

    name = "fake_fetcher"

    def __init__(self, body: str) -> None:
        self.body = body
        self.urls: list[str] = []

    async def fetch(self, url: str, ctx: PortCtx) -> str:
        self.urls.append(url)
        return self.body


class FakeTransport:
    """fake MCP 客户端（stdio/http 替身），实现 R5 本地 MCPTransport 协议。"""

    name = "fake_transport"

    def __init__(self) -> None:
        self.call_log: list[tuple[str, str]] = []

    async def list_tools(self, server: str) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search", description="搜索",
                parameters_schema={"type": "object"},
                danger=compose.DangerLevel.SAFE, source="mcp",
            ),
        ]

    async def call_tool(self, server: str, tool: str, args: dict[str, object]) -> object:
        # 参数名须与 MCPTransport 协议一致（结构化子类型按名匹配，改名会履约失败）。
        self.call_log.append((server, tool))
        return f"{server}/{tool}"

    async def ping(self, server: str) -> bool:
        return server == "alive"


@pytest.fixture(autouse=True)
def clean_pending_tools() -> None:
    """每用例前清空 @tool 待注册容器（v2.2），防全局状态跨测试污染。"""
    clear_pending()


@pytest.fixture
def ctx() -> PortCtx:
    """统一的 Port 调用上下文。"""
    return PortCtx(session_id="s1", run_id="r1")


@pytest.fixture
def sandbox() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def human() -> FakeHuman:
    return FakeHuman()


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()
