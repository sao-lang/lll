"""R5-T03 + R5-T07：danger 四级路由各生效 + 构造注入缺席即拒绝。

权威语义（R9 §5）：SAFE=直执行 / LOW=审计后直执行 / HIGH=沙盒 / CRITICAL=审批后沙盒。
"""
from __future__ import annotations

import asyncio

from compose import DangerLevel, PortCtx, make_call
from harness.tools import ToolFn, ToolRegistry, tool

from .conftest import FakeAudit, FakeHuman, FakeSandbox


def _fn(danger: DangerLevel, value: object = "direct") -> ToolFn:
    """构造固定返回值的工具，供路由测试。"""
    @tool(name="run", danger=danger)
    def run() -> object:
        return value
    return run


def _call(registry: ToolRegistry, ctx: PortCtx, **kwargs: object) -> object:
    """统一的调用入口（伪造 danger 用）。"""
    return asyncio.run(registry.call(make_call("run", {}, **kwargs), ctx))


def test_safe_executes_directly(ctx: PortCtx) -> None:
    """SAFE：无需任何 R9 port，直接执行函数。"""
    registry = ToolRegistry()
    fn = _fn(DangerLevel.SAFE)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and result.value == "direct"
    assert result.meta["sandbox"] is False and result.meta["approved"] is False


def test_low_audits_then_direct(ctx: PortCtx, audit: FakeAudit) -> None:
    """LOW：先记审计再直执行；审计事件带 danger/source。"""
    registry = ToolRegistry(audit=audit)
    fn = _fn(DangerLevel.LOW)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and result.value == "direct"
    assert len(audit.events) == 1
    assert audit.events[0]["danger"] == "LOW"


def test_low_without_audit_degrades_with_warning(ctx: PortCtx) -> None:
    """LOW 审计缺席：降级直执行并在 meta 标记 audit_missing（不拒绝、不静默）。"""
    registry = ToolRegistry()
    fn = _fn(DangerLevel.LOW)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and result.meta.get("audit_missing") is True


def test_high_uses_sandbox(ctx: PortCtx, sandbox: FakeSandbox) -> None:
    """HIGH：经 SandboxPort 执行，meta 标 sandbox=True。"""
    registry = ToolRegistry(sandbox=sandbox)
    fn = _fn(DangerLevel.HIGH)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and sandbox.calls == ["run"]
    assert result.meta["sandbox"] is True


def test_high_without_sandbox_refuses(ctx: PortCtx) -> None:
    """HIGH 沙盒缺席：拒绝执行（不抛穿），error 标 NO_SANDBOX。"""
    registry = ToolRegistry()
    fn = _fn(DangerLevel.HIGH)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "NO_SANDBOX"


def test_critical_approval_then_sandbox(
    ctx: PortCtx, human: FakeHuman, sandbox: FakeSandbox,
) -> None:
    """CRITICAL：先人工审批再沙盒执行，两 Port 都被真实调用。"""
    registry = ToolRegistry(human=human, sandbox=sandbox)
    fn = _fn(DangerLevel.CRITICAL)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and len(human.questions) == 1 and sandbox.calls == ["run"]
    assert result.meta["approved"] is True and result.meta["sandbox"] is True


def test_critical_denied_approval_skips_sandbox(
    ctx: PortCtx, sandbox: FakeSandbox,
) -> None:
    """CRITICAL 审批拒绝：不进沙盒，error 标 APPROVAL_DENIED。"""
    registry = ToolRegistry(human=FakeHuman(answer="deny"), sandbox=sandbox)
    fn = _fn(DangerLevel.CRITICAL)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "APPROVAL_DENIED" and sandbox.calls == []


def test_critical_without_human_refuses(ctx: PortCtx, sandbox: FakeSandbox) -> None:
    """CRITICAL 缺 HumanPort：拒绝（NO_HUMAN），即便沙盒在场。"""
    registry = ToolRegistry(sandbox=sandbox)
    fn = _fn(DangerLevel.CRITICAL)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "NO_HUMAN"


def test_call_danger_cannot_downgrade(ctx: PortCtx, sandbox: FakeSandbox) -> None:
    """安全：调用方伪造 danger=SAFE 不能降级已注册的高危工具（以注册 spec 为准）。"""
    registry = ToolRegistry(sandbox=sandbox)
    fn = _fn(DangerLevel.HIGH)
    registry.register(fn.spec, fn)
    forged = make_call("run", {}, danger=DangerLevel.SAFE)
    result = asyncio.run(registry.call(forged, ctx))
    assert sandbox.calls == ["run"] and result.meta["danger"] == "HIGH"
