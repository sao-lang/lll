"""R9-T04 HITL 审批流：挂起 → 跨周期恢复 → 精确续跑。"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.security import ApprovalFlow, ConsoleHumanPort
from runtime import Result, Runner, Session, Suspend, Terminate


def _ctx() -> PortCtx:
    return PortCtx(session_id="s9", run_id="r9")


def test_t04_console_human_approve() -> None:
    """ConsoleHumanPort.ask 从 options 取答案（测试注入 answerer，不读真终端）。"""
    port = ConsoleHumanPort(answerer=lambda q, opts: "approve")
    assert asyncio.run(port.ask("ok?", ["approve", "deny"], _ctx())) == "approve"


def test_t04_approval_flow_trigger() -> None:
    """ApprovalFlow 对 CRITICAL 触发、对 SAFE 不触发（trigger_danger 可配）。"""
    flow = ApprovalFlow(trigger_danger="CRITICAL")
    from compose import DangerLevel
    assert flow.should_approve(DangerLevel.CRITICAL)
    assert not flow.should_approve(DangerLevel.SAFE)
    assert not flow.should_approve(DangerLevel.HIGH)  # HIGH 只沙盒不审批


def test_t04_approval_timeout_denies() -> None:
    """超时按拒答处理（不静默放行）。"""
    flow = ApprovalFlow(timeout=0)
    assert flow.decide_timeout() == "deny"


def test_t04_suspend_resume_exact() -> None:
    """真实 Runner：Program yield Suspend 挂起，resume 后从挂起点精确续跑。"""
    seen = []

    def program():
        answer = yield Suspend(question="approve?", options=("yes", "no"))
        seen.append(answer)
        yield Terminate(output=answer)
        return None

    runner = Runner(call=lambda e, c: Result.success(None))
    session = Session(session_id="s9-hitl", program_factory=lambda: program())
    suspended = asyncio.run(runner.run(session))
    assert suspended.status == "suspended"
    resumed = asyncio.run(runner.resume(session, "yes"))
    assert resumed.status == "done"
    assert resumed.output == "yes"
    assert seen == ["yes"]
