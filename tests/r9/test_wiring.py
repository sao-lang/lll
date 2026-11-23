"""R9 接线：ToolsCapability 注入 R9 三 Port + SecurityCapability 注册（端到端）。"""
from __future__ import annotations

import asyncio

from compose import DangerLevel, PortCtx, Registry, ToolSpec, make_call
from harness.security import AuditLogger, ConsoleHumanPort, LocalSandbox, SecurityCapability
from harness.tools import ToolsCapability


def test_capability_installs_three_ports() -> None:
    """SecurityCapability.install 按 PORT_PROTOCOLS 键注册 sandbox/audit/human。"""
    reg = Registry()
    cap = SecurityCapability()
    cap.install(object(), reg)  # 标准 Capability.install(container, reg) 签名
    assert reg.has("sandbox") and reg.has("audit") and reg.has("human")
    assert isinstance(reg.resolve("sandbox"), LocalSandbox)
    assert isinstance(reg.resolve("audit"), AuditLogger)
    assert isinstance(reg.resolve("human"), ConsoleHumanPort)


def test_tools_capability_wiring_with_r9_ports() -> None:
    """接线：SecurityCapability 三 Port 注入 ToolsCapability 后，HIGH 工具真进沙盒。"""
    audit = AuditLogger()
    cap = SecurityCapability(audit=audit, human=ConsoleHumanPort(answerer=lambda q, o: "approve"))
    tools = ToolsCapability(sandbox=cap.sandbox, human=cap.human, audit=cap.audit)
    reg = Registry()
    tools.install(object(), reg)
    registry = reg.resolve("tool")
    registry.register(ToolSpec(name="hw", description="", danger=DangerLevel.HIGH),
                      lambda **kw: "sandbox-ok")
    res = asyncio.run(registry.call(make_call("hw", {}), PortCtx(session_id="s9", run_id="r9")))
    assert res.ok and res.value == "sandbox-ok"
    assert res.meta.get("sandbox") is True
    assert len(audit.events()) == 1 and "danger=HIGH" in audit.events()[0].rationale  # HIGH 必写审计
