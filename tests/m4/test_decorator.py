"""M4-T01/T02 @tool → JSON Schema 与装配期 static_check。"""
from __future__ import annotations

from enum import Enum

import pytest
from pydantic import BaseModel, Field

from harness.tools import (
    DangerLevel, SandboxPolicy, ToolDefinition, ToolDefinitionError,
    ToolRegistry, tool,
)
from harness.tools.decorator import _schema_of
from ports import PortCtx, ToolSpec


class Mode(str, Enum):
    FAST = "fast"
    SLOW = "slow"


class Nested(BaseModel):
    locale: str = "zh-CN"


class Args(BaseModel):
    text: str = Field(..., description="文本")
    count: int = Field(1, gt=0, le=5)
    mode: Mode = Mode.FAST
    nested: Nested | None = None


@tool(name="demo", danger=DangerLevel.SAFE, description="自定义描述")
def demo(args: Args, ctx: PortCtx):
    """这行 docstring 不应覆盖显式 description。"""
    return args.text * args.count


@tool(name="doc", danger="low")
def doc(args: Args, ctx: PortCtx):
    """取自 docstring 的一行描述。"""
    return "ok"


def test_t01_schema_maps_constraints_enum_and_nesting():
    schema = demo.__agentkit_tool__.spec.parameters_schema
    props = schema["properties"]
    assert props["text"]["description"] == "文本"
    assert props["count"]["exclusiveMinimum"] == 0 and props["count"]["maximum"] == 5
    assert "$defs" in schema and "Nested" in schema["$defs"]
    assert schema["$defs"]["Mode"]["enum"] == ["fast", "slow"]
    assert schema["additionalProperties"] is False
    assert _schema_of(None)["properties"] == {}


def test_t01_description_and_danger():
    assert demo.__agentkit_tool__.spec.description == "自定义描述"
    assert doc.__agentkit_tool__.spec.description == "取自 docstring 的一行描述。"
    assert doc.__agentkit_tool__.spec.danger is DangerLevel.LOW


def test_t01_decorate_model_then_bind():
    @tool(name="model-tool", danger=DangerLevel.SAFE)
    class ModelArgs(BaseModel):
        q: str

    definition = ModelArgs.__agentkit_tool__
    assert isinstance(definition, ToolDefinition) and definition.fn is None
    registry = ToolRegistry()
    # 装饰模型类只产出定义，实现由 register(definition, fn) 或 bind 注入
    registry.register(definition, lambda args, ctx: args.q)
    assert registry.catalog(level=2)[0].name == "model-tool"
    registry.bind("model-tool", lambda args, ctx: "rebound")
    from ports import ToolCall
    from tests.fakes import run
    bound = run(registry.call(ToolCall(call_id="c", name="model-tool", args={"q": "x"}), PortCtx("s")))
    assert bound.ok and bound.value == "rebound"


def test_t02_danger_missing_and_invalid_targets_rejected():
    with pytest.raises(ToolDefinitionError, match="danger"):
        tool(name="no-danger")
    with pytest.raises(ToolDefinitionError, match="function or a pydantic"):
        tool(danger=DangerLevel.SAFE)(42)
    with pytest.raises(ToolDefinitionError, match="BaseModel"):
        tool(danger=DangerLevel.SAFE, args=int)(lambda args, ctx: None)


def test_t02_static_check_and_duplicate_registration():
    registry = ToolRegistry()
    registry.add(demo.__agentkit_tool__)
    assert registry.static_check() == []
    assert registry.freeze() is registry
    with pytest.raises(ToolDefinitionError, match="duplicate"):
        registry.add(demo.__agentkit_tool__)
    with pytest.raises(ToolDefinitionError, match="danger"):
        registry.register(ToolSpec(name="unsafe"), lambda a, c: None)


def test_t02_static_check_reports_invalid_schema():
    registry = ToolRegistry().register(
        ToolSpec(name="bad", danger=DangerLevel.SAFE, parameters_schema={"foo": "bar"}),
        lambda a, c: None,
    )
    assert any("invalid parameters_schema" in e for e in registry.static_check())
    with pytest.raises(ToolDefinitionError, match="static_check failed"):
        registry.freeze()


def test_t02_extra_forbid_and_bind_errors():
    from ports import ToolCall
    from tests.fakes import run

    registry = ToolRegistry().add(demo.__agentkit_tool__)
    out = run(registry.call(ToolCall(call_id="c", name="demo",
                                     args={"text": "x", "unknown": 1}), PortCtx("s")))
    assert not out.ok and "unexpected arguments" in out.error
    with pytest.raises(ToolDefinitionError, match="unknown tool"):
        registry.bind("missing", lambda a, c: None)
    with pytest.raises(ToolDefinitionError, match="!="):
        registry.bind("other", demo)
    assert SandboxPolicy.AUTO.value == "auto"
