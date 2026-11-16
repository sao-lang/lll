"""M4-T06 工具异常折叠、参数校验、注册错误、与全链路集成。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from chains import Chains, MountPoint
from chains.builtins import AllowFilter
from harness.tools import DangerLevel, ToolDefinitionError, ToolRegistry, tool
from ports import PortCtx, PortRegistry, ToolCall, ToolSpec, make_call
from runtime import AgentState, Effect, EffectKind, Runner, Session
from tests.fakes import run


class Args(BaseModel):
    value: int = Field(..., gt=0)


@tool(name="boom", danger=DangerLevel.SAFE)
def boom(args: Args, ctx: PortCtx):
    raise RuntimeError("tool exploded")


@tool(name="echo", danger=DangerLevel.SAFE)
def echo(args: Args, ctx: PortCtx):
    return f"v={args.value}"


@tool(name="async_boom", danger=DangerLevel.SAFE)
async def async_boom(args: Args, ctx: PortCtx):
    raise ValueError("async exploded")


def test_t06_exceptions_are_folded_never_raised():
    registry = ToolRegistry().add(boom.__agentkit_tool__).add(async_boom.__agentkit_tool__)
    for name, marker in (("boom", "RuntimeError"), ("async_boom", "ValueError")):
        out = run(registry.call(ToolCall(call_id="c", name=name, args={"value": 1}), PortCtx("s")))
        assert not out.ok and marker in out.error and out.meta["sandbox"] is False


def test_t06_invalid_args_and_unknown_tool():
    registry = ToolRegistry().add(echo.__agentkit_tool__)
    invalid = run(registry.call(ToolCall(call_id="c", name="echo", args={"value": 0}), PortCtx("s")))
    assert not invalid.ok and invalid.error.startswith("invalid args")
    extra = run(registry.call(ToolCall(call_id="c", name="echo",
                                       args={"value": 1, "x": 2}), PortCtx("s")))
    assert not extra.ok and "unexpected arguments" in extra.error
    unknown = run(registry.call(ToolCall(call_id="c", name="nope", args={}), PortCtx("s")))
    assert not unknown.ok and "unknown tool" in unknown.error
    with pytest.raises(ToolDefinitionError):
        registry.schema_of("nope")


def test_t06_registration_errors():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="x", danger=DangerLevel.SAFE,
                               parameters_schema={"type": "object", "properties": {}}),
                      lambda a, c: None)
    with pytest.raises(ToolDefinitionError, match="duplicate"):
        registry.register(ToolSpec(name="x", danger=DangerLevel.SAFE), lambda a, c: None)
    with pytest.raises(ToolDefinitionError, match="no implementation"):
        registry.register(ToolSpec(name="y", danger=DangerLevel.SAFE))
    with pytest.raises(ToolDefinitionError, match="bound name"):
        registry.bind("x", echo)
    assert registry.catalog(level=2)[0].parameters_schema["type"] == "object"


def test_t06_integrated_tool_use_with_m1_m2_m3():
    registry = ToolRegistry().add(echo.__agentkit_tool__).freeze()
    ports = PortRegistry().register("tool", registry).freeze(("tool",))
    chains = Chains().filter(AllowFilter("guard"), at=MountPoint.BEFORE_TOOL).freeze(("tool",))

    def tool_use(state):
        results = yield Effect(EffectKind.TOOL, {"calls": [
            {"call_id": "c1", "name": "echo", "args": {"value": 1}},
            {"call_id": "c2", "name": "echo", "args": {"value": 2}},
        ]}, batch=True)
        yield Effect(EffectKind.END, {"output": [r.value for r in results]})

    runner = Runner(dispatch=chains.dispatch, call=make_call(ports), ctx_factory=chains.ctx_factory())
    result = run(runner.run(Session("tooluse", lambda: tool_use(AgentState()))))
    assert result.status == "done", (
        f"error={result.error!r} reason={result.reason!r} "
        f"results={[(r.ok, r.error) for r in result.events[0].results]}"
    )
    assert result.output == ["v=1", "v=2"]
    assert result.events[0].batched and len(result.events[0].effects) == 2
