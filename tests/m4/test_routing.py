"""M4-T05 danger 四级路由：SAFE 直执行 / LOW,HIGH 沙盒 / CRITICAL 审批。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from harness.tools import (
    DangerLevel, SandboxPolicy, ToolDefinitionError, ToolRegistry, tool,
)
from tests.fakes.m4 import FakeSandbox, StaticApprover
from ports import PortCtx, ToolCall, ToolSpec
from tests.fakes import run


class Args(BaseModel):
    value: int = Field(1, gt=0)


@tool(name="safe", danger=DangerLevel.SAFE)
def safe(args: Args, ctx: PortCtx):
    return "safe"


@tool(name="low", danger=DangerLevel.LOW)
def low(args: Args, ctx: PortCtx):
    return "low"


@tool(name="high", danger=DangerLevel.HIGH)
def high(args: Args, ctx: PortCtx):
    return "high"


@tool(name="critical", danger=DangerLevel.CRITICAL)
def critical(args: Args, ctx: PortCtx):
    return "critical"


def build(*, approve=True):
    sandbox = FakeSandbox()
    approver = StaticApprover(approve)
    registry = ToolRegistry(sandbox=sandbox, approver=approver, clock=lambda: 0.0)
    for fn in (safe, low, high, critical):
        registry.add(fn.__agentkit_tool__)
    return registry, sandbox, approver


def test_t05_four_levels_route_correctly():
    registry, sandbox, approver = build()
    outcomes = {name: run(registry.call(
        ToolCall(call_id=name, name=name, args={"value": 1}), PortCtx("s")))
        for name in ("safe", "low", "high", "critical")}
    assert all(r.ok for r in outcomes.values())
    assert not outcomes["safe"].meta["sandbox"]
    assert outcomes["low"].meta["sandbox"] and outcomes["high"].meta["sandbox"]
    assert outcomes["critical"].meta["sandbox"] and outcomes["critical"].meta["approved"]
    assert len(sandbox.runs) == 3 and len(approver.questions) == 1
    assert all(r.meta["duration_ms"] == 0.0 and "source" in r.meta for r in outcomes.values())


def test_t05_approval_denied_never_executes():
    registry, sandbox, _ = build(approve=False)
    out = run(registry.call(ToolCall(call_id="c", name="critical", args={"value": 1}), PortCtx("s")))
    assert not out.ok and out.error == "approval denied" and out.meta["approved"] is False
    assert sandbox.runs == []


def test_t05_registration_rejects_unsafe_configuration():
    with pytest.raises(ToolDefinitionError, match="SandboxPort"):
        ToolRegistry().add(high.__agentkit_tool__)
    with pytest.raises(ToolDefinitionError, match="approval port"):
        ToolRegistry(sandbox=FakeSandbox()).add(critical.__agentkit_tool__)
    # static_check 同样能查出缺失（绕过注册期检查直接注入条目）
    from harness.tools.registry import ToolEntry
    weak = ToolRegistry(sandbox=None, approver=None)
    weak._tools["high"] = ToolEntry(high.__agentkit_tool__.spec, high)
    assert any("SandboxPort" in e for e in weak.static_check())


def test_t05_always_policy_routes_even_safe_tools():
    @tool(name="always", danger=DangerLevel.SAFE, sandbox=SandboxPolicy.ALWAYS)
    def always(args: Args, ctx: PortCtx):
        return "always"

    sandbox = FakeSandbox()
    registry = ToolRegistry(sandbox=sandbox).add(always.__agentkit_tool__)
    out = run(registry.call(ToolCall(call_id="c", name="always", args={"value": 1}), PortCtx("s")))
    assert out.ok and out.meta["sandbox"] and len(sandbox.runs) == 1


def test_t05_async_tool_function_and_human_port_ask_path():
    @tool(name="async", danger=DangerLevel.LOW)
    async def async_tool(args: Args, ctx: PortCtx):
        return f"async:{args.value}"

    class AskOnly:
        name = "ask-only"

        async def ask(self, question, options, ctx):
            return "yes"

    sandbox = FakeSandbox()
    registry = ToolRegistry(sandbox=sandbox, approver=AskOnly())
    registry.add(async_tool.__agentkit_tool__)
    out = run(registry.call(ToolCall(call_id="c", name="async", args={"value": 2}), PortCtx("s")))
    assert out.ok and out.value == "async:2" and out.meta["sandbox"]

    @tool(name="crit-ask", danger=DangerLevel.CRITICAL)
    def crit(args: Args, ctx: PortCtx):
        return "done"

    registry.add(crit.__agentkit_tool__)
    out2 = run(registry.call(ToolCall(call_id="c", name="crit-ask", args={"value": 1}), PortCtx("s")))
    assert out2.ok and out2.meta["approved"]


def test_t05_no_approver_returns_false():
    from harness.tools.registry import ToolEntry
    registry = ToolRegistry(sandbox=FakeSandbox())
    registry._tools["x"] = ToolEntry(
        ToolSpec(name="x", danger=DangerLevel.SAFE,
                 parameters_schema={"type": "object", "properties": {}}),
        lambda a, c: None,
    )
    assert run(registry._approve(registry._tools["x"].spec, PortCtx("s"))) is False
