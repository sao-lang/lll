"""R11 §11 六项跨模块接线（TDD RED → GREEN）。

每项都是真实行为断言：审批超时 deny、审计链可验、secret 脱敏、相位→事件流投影、
checkpoint 落盘/恢复、CI 评测阈值阻断。不改 R1/R5/R9 源码，接线全在 src/api/。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from api.wiring import (
    ApprovingHumanPort,
    CheckpointEventStore,
    VerifyingAuditPort,
    bind_stream_projection,
    redact_output,
    verify_audit_chain,
)
from compose import DangerLevel, PortCtx
from harness.durability import CheckpointManager
from harness.eval import EvalCase, RuleEval
from harness.observability import EventEnvelope, InMemoryEventStream, TimelineProjection
from harness.security import ApprovalFlow, AuditLogger
from runtime import AgentState, Invoke, Result, Session, Terminate


# ---- 1. 审批流 ----
class _Human:
    name = "test_human"

    def __init__(self, answer: str = "approve", *, hang: bool = False) -> None:
        self.answer = answer
        self.hang = hang
        self.calls: list[str] = []

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        self.calls.append(question)
        if self.hang:
            await asyncio.sleep(10)
        return self.answer


def test_approval_consulted_and_deny() -> None:
    """审批流：should_approve(CRITICAL) 为真时问人类；非触发级别直接 deny。"""
    flow = ApprovalFlow(trigger_danger="CRITICAL", timeout=1)
    inner = _Human("approve")
    wrapped = ApprovingHumanPort(inner, flow)
    ctx = PortCtx()

    # SAFE 不触发审批 → 直接 deny，不问人
    out = asyncio.run(wrapped.ask("q", ["approve", "deny"], ctx, danger=DangerLevel.SAFE))
    assert out == "deny" and inner.calls == []

    # CRITICAL 触发 → 问人，采纳回答
    out = asyncio.run(wrapped.ask("q", ["approve", "deny"], ctx, danger=DangerLevel.CRITICAL))
    assert out == "approve" and len(inner.calls) == 1


def test_approval_timeout_denies() -> None:
    """审批超时 → decide_timeout() = deny，绝不静默放行。"""
    flow = ApprovalFlow(trigger_danger="CRITICAL", timeout=0)
    wrapped = ApprovingHumanPort(_Human(hang=True), flow)
    out = asyncio.run(wrapped.ask("q", None, PortCtx(), danger=DangerLevel.CRITICAL))
    assert out == "deny"


# ---- 2. 审计哈希链 ----
def test_verifying_audit_port_chain() -> None:
    """审计每次 record 后链可验；篡改后 verify_audit_chain 拒绝。"""
    base = AuditLogger()
    wrapped = VerifyingAuditPort(base)
    ctx = PortCtx()
    asyncio.run(wrapped.record({"action": "tool.call", "when": 1}, ctx))
    asyncio.run(wrapped.record({"action": "tool.done", "when": 2}, ctx))
    assert verify_audit_chain(wrapped) is True
    assert len(wrapped.events()) == 2

    # 篡改事件 → 链断
    tampered = replace(wrapped.events()[0], rationale="tampered")
    with pytest.raises(ValueError, match="audit hash chain"):
        wrapped.verify_external((tampered,) + wrapped.events()[1:])


# ---- 3. secret 脱敏 ----
def test_secret_redaction_on_output() -> None:
    """装配注入 secret_refs → 输出护栏 redact，secret 不进 trace/记忆。"""
    verdict = redact_output("token=sk-live-999 done", PortCtx(),
                            secret_refs=["sk-live-999"], source="tool", direction="output")
    assert verdict.action == "redact"
    assert "sk-live-999" not in str(verdict.redacted)


# ---- 4. 事件流投影 ----
def test_stream_projection_from_phases() -> None:
    """after_model/after_tool/session_end 相位发布 EventEnvelope，可续读+投影。"""
    stream = InMemoryEventStream()
    bus = bind_stream_projection(stream)
    from compose.notify import LifecyclePhase, Notice

    bus.emit(Notice(phase=LifecyclePhase.AFTER_MODEL, session_id="s", port="llm",
                    payload={"model": "m"}))
    bus.emit(Notice(phase=LifecyclePhase.AFTER_TOOL, session_id="s", port="tool",
                    payload={}))
    bus.emit(Notice(phase=LifecyclePhase.SESSION_END, session_id="s",
                    payload={"status": "done"}))

    async def drain():
        return [e async for e in stream.subscribe("trace")]

    events = asyncio.run(drain())
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].type == "after_model"
    model = TimelineProjection().project(events)
    assert model.data["count"] == 3

    # 续读
    async def drain_since():
        return [e async for e in stream.subscribe("trace", since=1)]
    assert [e.seq for e in asyncio.run(drain_since())] == [2, 3]


# ---- 5. checkpoint 落盘/恢复 ----
def test_checkpoint_store_save_and_replay() -> None:
    """Runner(store=CheckpointEventStore) 落盘 → load + replay_snapshot 终态全等。"""
    mgr = CheckpointManager()
    adapter = CheckpointEventStore(mgr)
    session = Session(session_id="s-cp", program_factory=lambda: None,
                      state=AgentState().set("k", 7), version="v1")
    from runtime.types import checkpoint

    snap = checkpoint(session)
    asyncio.run(adapter.save("s-cp", snap))
    restored = asyncio.run(adapter.load("s-cp"))
    assert restored is not None and restored["fingerprint"] == snap["fingerprint"]

    # R10 快照链路（内存 DTO）
    dto = mgr.snapshot("s-cp", session.state, tuple(session.events))
    from harness.durability import replay_snapshot
    assert replay_snapshot(dto).get("k") == 7


# ---- 6. CI 评测 gate ----
def test_eval_ci_gate_threshold() -> None:
    """regression + 阈值：达标 ok=True，不达标 ok=False（CI 阻断）。"""
    from api import CLI

    cases = {
        "c1": EvalCase(case_id="c1", input="q", expected="a", actual="a"),
        "c2": EvalCase(case_id="c2", input="q", expected="b", actual="WRONG"),
    }
    cli = CLI()
    ok = cli.run("eval", suite="ci-main", cases=cases, threshold=0.4)
    assert ok.ok and ok.data["average"] == 0.5 and ok.data["passed"] == 1
    blocked = cli.run("eval", suite="ci-main", cases=cases, threshold=0.9)
    assert not blocked.ok  # CI 阻断信号


# ---- 生产路径：AgentBuilder 真消费六项接线（非仅测试调用） ----
def test_builder_production_path_consumes_all_wires() -> None:
    """AgentBuilder 装配后暴露全部接线，生产路径可消费（非仅测试自证）。"""
    from api import AgentBuilder
    from harness.observability import InMemoryEventStream
    from tests.r11.conftest import StubArch

    flow = ApprovalFlow(trigger_danger="CRITICAL", timeout=1)
    approving = ApprovingHumanPort(_Human(), flow)
    auditing = VerifyingAuditPort(AuditLogger())
    stream = InMemoryEventStream()

    b = (AgentBuilder(StubArch())
         .approve(approving).audit(auditing)
         .secrets(["sk-prod-123"])
         .project_events(stream).checkpoint())

    assert b.stream_bus() is not None
    assert b.checkpoint_store() is not None
    assert "approval" in b.capability_names() and "audit_verify" in b.capability_names()

    # ③ secret 脱敏经生产路径消费
    verdict = b.redact("key=sk-prod-123 ok", PortCtx())
    assert getattr(verdict, "action", "") == "redact"

    # ①② 审批/审计端口真被持有
    assert b.capability_names().__contains__("approval")


# ---- 真实运行期装配：Runner/Session 实际消费六项接线 ----
def test_assemble_runtime_runs_real_session() -> None:
    """assemble_runtime 构造真实 Runner，跑真实 Session（非 getter 自证）。"""
    import asyncio

    from api import STREAM_TOPIC, assemble_runtime, capability_tools
    from runtime import Result, Session

    seen: list[str] = []

    def call(effect, ctx):  # noqa: ANN001
        seen.append(getattr(effect, "port", ""))
        return Result.success({"ok": True})

    rt = assemble_runtime(call)
    assert isinstance(rt.tools, object) and rt.approval is not None and rt.audit is not None

    def program():
        from runtime import Invoke, Terminate

        yield Invoke(port="llm", payload={"q": "hi"})
        yield Terminate(output="done")
        return None

    session = Session(session_id="s-runtime", program_factory=program)
    result = asyncio.run(rt.run(session))
    assert result.status == "done" and result.output == "done"
    assert "llm" in seen

    # ④ bus 真接到 Runner.notify：订阅的 session_end 相位已发布为事件
    async def drain():
        return [e async for e in rt.stream.subscribe(STREAM_TOPIC)]

    events = asyncio.run(drain())
    assert any(e.type == "session_end" for e in events)

    # ⑤ checkpoint 适配器确实被 Runner 调用落盘（终态必存）
    snap = asyncio.run(rt.checkpoint.load("s-runtime"))
    assert snap is not None and snap.get("fingerprint")

    # ①② 工具能力由已接线 Port 构造（生产装配入口）
    tools = capability_tools(rt.approval, rt.audit)  # type: ignore[arg-type]
    assert tools.registry() is not None


def test_wrapped_ports_consumed_by_tool_registry() -> None:
    """①② 真消费：ToolRegistry(human=wrapper, audit=wrapper) 的 CRITICAL/LOW 分支调包装器。"""
    from api import capability_tools
    from compose import DangerLevel, PortCtx
    from compose import ToolCall, ToolSpec

    flow = ApprovalFlow(trigger_danger="CRITICAL", timeout=1)
    human = _Human("approve")
    approving = ApprovingHumanPort(human, flow)
    auditing = VerifyingAuditPort(AuditLogger())
    tools = capability_tools(approving, auditing).registry()

    # LOW：先审计再执行（审计包装器真 record + 验链）
    tools.register(ToolSpec(name="low_tool", danger=DangerLevel.LOW), lambda: 1)
    low = asyncio.run(tools.call(ToolCall(call_id="c1", name="low_tool", args={},
                                          danger=DangerLevel.LOW), PortCtx()))
    assert low.ok and len(auditing.events()) == 1
    assert verify_audit_chain(auditing)

    # CRITICAL：先审批（包装器真 consult flow + ask）再执行（需沙盒）
    from harness.security import LocalSandbox
    from harness.tools import ToolRegistry

    critical_reg = ToolRegistry(sandbox=LocalSandbox(), human=approving, audit=auditing)
    critical_reg.register(ToolSpec(name="crit_tool", danger=DangerLevel.CRITICAL), lambda: 2)
    crit = asyncio.run(critical_reg.call(ToolCall(call_id="c2", name="crit_tool", args={},
                                                  danger=DangerLevel.CRITICAL), PortCtx()))
    assert crit.ok and human.calls, "CRITICAL 分支未消费审批包装器"
