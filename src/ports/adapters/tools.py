"""M3 最小工具注册表（M4 的 ToolRegistry 实现同一 ToolPort，可整体替换）。

工具异常统一折叠为 ToolResult(ok=False)，由 M2 回程 filter 判决（红线：不抛穿）。
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from ..dto import ToolCall, ToolResult, ToolSpec
from ..registry import PortCtx

__all__ = ["InMemoryToolRegistry"]


class InMemoryToolRegistry:
    name = "in-memory-tool-registry"

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable[..., Any]]] = {}

    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = (spec, fn)

    def catalog(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    async def call(self, call: ToolCall, ctx: PortCtx) -> ToolResult:
        entry = self._tools.get(call.name)
        if entry is None:
            return ToolResult(call_id=call.call_id, ok=False, error=f"unknown tool: {call.name}", meta={"source": "local"})
        _, fn = entry
        try:
            value = fn(call.args, ctx)
            if inspect.isawaitable(value):
                value = await value
            return ToolResult(call_id=call.call_id, ok=True, value=value, meta={"source": "local"})
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id, ok=False,
                error=f"{type(exc).__name__}: {exc}",
                meta={"source": "local", "retryable": isinstance(exc, (TimeoutError, OSError))},
            )
