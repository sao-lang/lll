"""R5-T01：@tool 装饰器生成 schema + extra=forbid + danger 必填。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from compose import DangerLevel, make_call
from harness.tools import ToolFn, ToolRegistry, tool


class AddArgs(BaseModel):
    """加法参数模型（extra=forbid 由模型配置保证）。"""
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int


def test_tool_decorator_builds_spec() -> None:
    """装饰器从 pydantic 模型生成 ToolSpec：name/description/schema/danger/source 齐全。"""
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        """两数相加。"""
        return args.a + args.b

    assert isinstance(add, ToolFn)
    spec = add.spec
    assert spec.name == "add" and spec.description == "两数相加。"
    assert spec.danger is DangerLevel.SAFE and spec.source == "local"
    assert "a" in spec.parameters_schema.get("properties", {})


def test_tool_fn_validates_extra_forbid() -> None:
    """装饰后的可调用对象拒绝未声明参数（extra=forbid）。"""
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        return args.a + args.b

    assert add(a=1, b=2) == 3
    with pytest.raises(Exception):
        add(a=1, b=2, c=3)


def test_tool_decorator_requires_danger() -> None:
    """danger 是必填关键字参数（装配期强制，缺一即 TypeError）。"""
    with pytest.raises(TypeError):
        @tool(name="no_danger")  # type: ignore[call-arg]
        def no_danger(args: AddArgs) -> int:
            return args.a


def test_registry_register_and_call() -> None:
    """注册后可通过 registry.call 统一调用，结果包成 ToolResult。"""
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        return args.a + args.b

    registry = ToolRegistry()
    registry.register(add.spec, add)
    result = __import__("asyncio").run(
        registry.call(make_call("add", {"a": 2, "b": 3}), __import__("compose").PortCtx())
    )
    assert result.ok and result.value == 5
    assert result.meta["danger"] == "SAFE" and result.meta["source"] == "local"
