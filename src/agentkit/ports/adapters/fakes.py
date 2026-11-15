"""测试替身（CL-2：M3 自带 fake，独立闭环不依赖真实服务）。

注意：这里允许脚本游标/缓冲等测试状态；M3「适配器无状态」红线约束的是
正式适配器（llm.py / tools.py / telemetry.py / human.py），fake 不参与门禁扫描。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator

from agentkit.runtime import Usage

from ..dto import LLMResponse
from ..registry import PortCtx
from .telemetry import TraceSpan

__all__ = ["ScriptedLLM", "StaticHuman", "InMemoryTelemetry", "StaticSkillPort", "FailingAdapter"]


class ScriptedLLM:
    name = "scripted-llm"

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._responses = list(responses or [LLMResponse(content="ok", model="fake")])
        self._cursor = 0
        self.requests: list[Any] = []

    async def complete(self, req: Any, ctx: PortCtx) -> LLMResponse:
        self.requests.append(req)
        item = self._responses[min(self._cursor, len(self._responses) - 1)]
        self._cursor += 1
        return item

    async def stream(self, req: Any, ctx: PortCtx) -> AsyncIterator[str]:  # pragma: no cover - P0 不用
        yield (await self.complete(req, ctx)).content or ""


class StaticHuman:
    name = "static-human"

    def __init__(self, answers: list[str] | None = None) -> None:
        self._answers = list(answers or ["approve"])
        self.questions: list[str] = []

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        self.questions.append(question)
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


class InMemoryTelemetry:
    name = "in-memory-telemetry"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def metric(self, name: str, value: float, tags: dict[str, Any] | None = None) -> None:
        self.log({"event": "metric", "name": name, "value": value, "tags": tags or {}})

    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[TraceSpan]:
        yield TraceSpan(name=name, session_id=ctx.session_id)


class StaticSkillPort:
    name = "static-skill-port"

    def __init__(self, bodies: dict[str, str] | None = None) -> None:
        self._bodies = bodies or {"python-expert": "你是一名 Python 专家。"}

    async def body(self, name: str, part: str, ctx: PortCtx) -> str:
        return self._bodies.get(name, "")


class FailingAdapter:
    """错误信封门禁用：抛出可重试异常 / 校验异常。"""

    name = "failing-adapter"

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or TimeoutError("upstream timeout")

    async def complete(self, req: Any, ctx: PortCtx) -> LLMResponse:
        raise self._exc

    async def call(self, payload: Any, ctx: PortCtx) -> Any:
        raise self._exc
