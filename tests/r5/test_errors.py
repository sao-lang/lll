"""R5-T05：工具异常折叠为 ToolResult，不抛穿。"""
from __future__ import annotations

import asyncio

from hooks import DangerLevel, PortCtx, make_call
from harness.tools import ToolRegistry, tool


def test_tool_exception_folded(ctx: PortCtx) -> None:
    """工具内部抛异常 → ToolResult(ok=False, error=TOOL_ERROR)，不外抛。"""
    @tool(name="boom", danger=DangerLevel.SAFE)
    def boom() -> None:
        raise RuntimeError("kaboom")

    registry = ToolRegistry()
    registry.register(boom.spec, boom)
    result = asyncio.run(registry.call(make_call("boom", {}), ctx))
    assert not result.ok
    assert result.error and "kaboom" in result.error


def test_unknown_tool_folded(ctx: PortCtx) -> None:
    """未注册工具 → ToolResult(ok=False, error=NOT_FOUND)，不抛 KeyError。"""
    registry = ToolRegistry()
    result = asyncio.run(registry.call(make_call("missing", {}), ctx))
    assert not result.ok and result.error == "NOT_FOUND"


def test_invalid_args_folded(ctx: PortCtx) -> None:
    """extra 参数在 registry 层也被拦截（不依赖装饰器），折叠为 INVALID_ARGS。"""
    @tool(name="echo", danger=DangerLevel.SAFE)
    def echo(**kwargs: object) -> object:
        return kwargs

    registry = ToolRegistry()
    registry.register(echo.spec, echo)
    result = asyncio.run(registry.call(make_call("echo", {"x": 1}), ctx))
    # echo 用 **kwargs 且 schema 为空：任何键都算 extra → INVALID_ARGS
    assert not result.ok and result.error == "INVALID_ARGS"
