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
    """LOW：先记审计再直执行；审计事件 rationale 带 danger/source（单一字段契约）。"""
    registry = ToolRegistry(audit=audit)
    fn = _fn(DangerLevel.LOW)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and result.value == "direct"
    assert len(audit.events) == 1
    ev = audit.events[0]
    assert ev["action"] == "tool.low.call"
    assert "danger=LOW" in ev["rationale"] and "source=local" in ev["rationale"]


def test_low_without_audit_fails_closed(ctx: PortCtx) -> None:
    """LOW 审计缺席：fail-closed 拒绝执行（S3 修复，不再静默降级放行）。"""
    registry = ToolRegistry()
    fn = _fn(DangerLevel.LOW)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "NO_AUDIT"


def test_high_uses_sandbox(ctx: PortCtx, sandbox: FakeSandbox, audit: FakeAudit) -> None:
    """HIGH：经 SandboxPort 执行，meta 标 sandbox=True；审计三级别覆盖。"""
    registry = ToolRegistry(sandbox=sandbox, audit=audit)
    fn = _fn(DangerLevel.HIGH)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and sandbox.calls == ["run"]
    assert result.meta["sandbox"] is True
    assert len(audit.events) == 1 and "danger=HIGH" in audit.events[0]["rationale"]


def test_high_without_sandbox_refuses(ctx: PortCtx, audit: FakeAudit) -> None:
    """HIGH 沙盒缺席：拒绝执行（不抛穿），error 标 NO_SANDBOX。"""
    registry = ToolRegistry(audit=audit)
    fn = _fn(DangerLevel.HIGH)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "NO_SANDBOX"


def test_critical_approval_then_sandbox(
    ctx: PortCtx, human: FakeHuman, sandbox: FakeSandbox, audit: FakeAudit,
) -> None:
    """CRITICAL：审批→沙盒→审计三道门全走，审计记 approved/sandbox 语义。"""
    registry = ToolRegistry(human=human, sandbox=sandbox, audit=audit)
    fn = _fn(DangerLevel.CRITICAL)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert result.ok and len(human.questions) == 1 and sandbox.calls == ["run"]
    assert result.meta["approved"] is True and result.meta["sandbox"] is True
    assert len(audit.events) == 1 and "approved=True" in audit.events[0]["rationale"]


def test_critical_denied_approval_skips_sandbox(
    ctx: PortCtx, sandbox: FakeSandbox, audit: FakeAudit,
) -> None:
    """CRITICAL 审批拒绝：不进沙盒，error 标 APPROVAL_DENIED，审计仍记拒绝决策。"""
    registry = ToolRegistry(human=FakeHuman(answer="deny"), sandbox=sandbox, audit=audit)
    fn = _fn(DangerLevel.CRITICAL)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "APPROVAL_DENIED" and sandbox.calls == []
    assert len(audit.events) == 1


def test_critical_without_human_refuses(
    ctx: PortCtx, sandbox: FakeSandbox, audit: FakeAudit,
) -> None:
    """CRITICAL 缺 HumanPort：拒绝（NO_HUMAN），即便沙盒在场。"""
    registry = ToolRegistry(sandbox=sandbox, audit=audit)
    fn = _fn(DangerLevel.CRITICAL)
    registry.register(fn.spec, fn)
    result = _call(registry, ctx)
    assert not result.ok and result.error == "NO_HUMAN"


def test_call_danger_cannot_downgrade(
    ctx: PortCtx, sandbox: FakeSandbox, audit: FakeAudit,
) -> None:
    """安全：调用方伪造 danger=SAFE 不能降级已注册的高危工具（以注册 spec 为准）。"""
    registry = ToolRegistry(sandbox=sandbox, audit=audit)
    fn = _fn(DangerLevel.HIGH)
    registry.register(fn.spec, fn)
    forged = make_call("run", {}, danger=DangerLevel.SAFE)
    result = asyncio.run(registry.call(forged, ctx))
    assert sandbox.calls == ["run"] and result.meta["danger"] == "HIGH"
