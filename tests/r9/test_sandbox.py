"""R9-T03 沙盒路由：HIGH+ 进沙盒，SAFE/LOW 直执行（R5 ToolRegistry 集成）。"""
from __future__ import annotations

import asyncio

from compose import DangerLevel, PortCtx, ToolSpec, make_call
from harness.security import LocalSandbox
from harness.tools import ToolRegistry


def _ctx() -> PortCtx:
    return PortCtx(session_id="s9", run_id="r9")


def test_t03_high_routes_to_sandbox() -> None:
    """HIGH 工具经 ToolRegistry 调用走沙盒（meta.sandbox=True）。"""
    sandbox = LocalSandbox()
    reg = ToolRegistry(sandbox=sandbox, human=None, audit=None)
    reg.register(ToolSpec(name="danger", description="d", danger=DangerLevel.HIGH),
                 lambda **kw: "executed")
    res = asyncio.run(reg.call(make_call("danger", {}), _ctx()))
    assert res.ok and res.value == "executed"
    assert res.meta.get("sandbox") is True


def test_t03_critical_without_human_denied() -> None:
    """CRITICAL 缺 HumanPort 直接拒绝（fail-closed，不静默放行）。"""
    reg = ToolRegistry(sandbox=LocalSandbox(), human=None, audit=None)
    reg.register(ToolSpec(name="critical", description="d", danger=DangerLevel.CRITICAL),
                 lambda **kw: "executed")
    res = asyncio.run(reg.call(make_call("critical", {}), _ctx()))
    assert not res.ok and res.error == "NO_HUMAN"


def test_t03_safe_direct() -> None:
    """SAFE 工具不经沙盒直执行。"""
    reg = ToolRegistry(sandbox=LocalSandbox(), audit=None)
    reg.register(ToolSpec(name="safe", description="d", danger=DangerLevel.SAFE),
                 lambda **kw: "ok")
    res = asyncio.run(reg.call(make_call("safe", {}), _ctx()))
    assert res.ok and res.meta.get("sandbox") is False
