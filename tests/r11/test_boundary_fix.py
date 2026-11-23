"""R5/R9/R10/R11 四项边界 + 两个 Blocker + 可明确修 Should（TDD RED → GREEN）。

每项失败测试对应契约 8ef577c1-r5r9r10r11-boundary-fix 的一条 FR：
- FR-1 审计三级别覆盖 + 字段填充无空壳 + 同源底座（R1 EventStore）
- FR-2 审计回放：从 R1 store 重建 AuditEvent 序列 + verify_hash_chain
- FR-3 EventEnvelope.ts 真墙钟（注入 clock）+ seq 来自 Notice 携带的 R1 序号
- FR-4 生产装配从 SecurityCapability 取 sandbox，CRITICAL 审批后真执行
- FR-5 Runner.notify 与 stream_bus getter 复用同一 bus
- FR-6 审计缺席 fail-closed
- FR-7 trigger_danger 生效（danger 透传到 ask）
- FR-8 ToolsCapability.install 两参协议
- FR-9 S7 单 store（audit sink 与 checkpoint 共享 R1 EventStore）
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from api import STREAM_TOPIC, assemble_runtime
from api.builder import AgentBuilder
from api.wiring import (
    ApprovingHumanPort,
    CheckpointEventStore,
    R1AuditEventSink,
    VerifyingAuditPort,
    bind_stream_projection,
    replay_audit_events,
    verify_audit_chain,
)
from compose import (
    DangerLevel,
    LifecyclePhase,
    Notice,
    PortCtx,
    Registry,
    ToolCall,
    ToolSpec,
)
from compose.notify import runner_notifier
from harness.durability import CheckpointManager
from harness.observability import EventEnvelope, InMemoryEventStream
from harness.security import (
    ApprovalFlow,
    AuditEvent,
    AuditLogger,
    ConsoleHumanPort,
    LocalSandbox,
    SecurityCapability,
    verify_hash_chain,
)
from harness.tools import ToolRegistry, ToolsCapability
from runtime import AgentState, InMemoryEventStore, JsonSnapshot, Session


# ---- 测试替身（fake 仅在 tests/，符合源码纯净铁律）----
class _ApproveHuman:
    name = "approve_human"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx) -> str:
        self.calls.append(question)
        return "approve"


class _RunSandbox:
    name = "run_sandbox"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, fn: object, args: object, ctx: PortCtx) -> object:
        self.calls.append("run")
        return fn(**(args if isinstance(args, dict) else {}))  # type: ignore[operator]


def _critical_tool() -> ToolSpec:
    return ToolSpec(name="deploy", danger=DangerLevel.CRITICAL)


def _call() -> ToolCall:
    return ToolCall(call_id="c1", name="deploy", args={}, danger=DangerLevel.CRITICAL)


async def _noop_call(effect: object, value: object) -> object:
    from runtime import Result
    return Result.ok(value=None)


def _drain(stream: InMemoryEventStream, topic: str) -> list[EventEnvelope]:
    async def go() -> list[EventEnvelope]:
        return [e async for e in stream.subscribe(topic)]  # type: ignore[misc]
    return asyncio.run(go())


# ---- FR-1 审计三级别覆盖 + 字段填充 ----
def test_audit_written_for_low_high_critical() -> None:
    """LOW/HIGH/CRITICAL 三分支都写审计（B1：不再只 LOW 写）。"""
    audit = AuditLogger()
    sandbox = _RunSandbox()
    human = _ApproveHuman()
    reg = ToolRegistry(sandbox=sandbox, human=human, audit=audit)  # type: ignore[arg-type]
    for level, name in ((DangerLevel.LOW, "low"), (DangerLevel.HIGH, "high"),
                        (DangerLevel.CRITICAL, "crit")):
        reg.register(ToolSpec(name=name, danger=level), lambda marker=name: marker)

    for level, name in ((DangerLevel.LOW, "low"), (DangerLevel.HIGH, "high"),
                        (DangerLevel.CRITICAL, "crit")):
        res = asyncio.run(reg.call(
            ToolCall(call_id=f"c-{name}", name=name, args={}, danger=level), PortCtx(session_id="s1")))
        assert res.ok, f"{name} 未执行成功: {res.error}"

    events = audit.events()
    assert len(events) == 3, f"三级别应各写一条审计，实际 {len(events)}"
    assert verify_hash_chain(events)


def test_audit_fields_not_empty_shell() -> None:
    """审计事件 who/when/action/data_touched/rationale 非空（B1 字段错配修复）。"""
    audit = AuditLogger()
    reg = ToolRegistry(audit=audit)
    reg.register(ToolSpec(name="low_tool", danger=DangerLevel.LOW), lambda: 1)
    asyncio.run(reg.call(ToolCall(call_id="c1", name="low_tool", args={}),
                         PortCtx(session_id="session-42")))
    event = audit.events()[0]
    assert event.who == "session-42", f"who 应来自 ctx.session_id，实际 {event.who!r}"
    assert event.when > 0, f"when 应是真实时钟，实际 {event.when}"
    assert event.action, "action 不能为空"
    assert "low_tool" in event.action or "low_tool" in str(event.data_touched), "data_touched/action 应含工具名"
    assert event.rationale, "rationale 应含 danger/source 语义，不能为空"


# ---- FR-2 审计同源底座 + 回放 ----
def test_audit_persists_to_r1_eventstore_and_replays() -> None:
    """审计事件落 R1 EventStore 保留 session；从同一 store 回放重建链（同源可回放）。"""
    store = InMemoryEventStore()
    sink = R1AuditEventSink(store)
    audit = AuditLogger(sink=sink)
    ctx = PortCtx(session_id="s-audit")
    asyncio.run(audit.record({"action": "tool.low", "when": 1}, ctx))
    asyncio.run(audit.record({"action": "tool.critical", "when": 2}, ctx))

    replayed = asyncio.run(replay_audit_events(sink, "s-audit"))
    assert len(replayed) == 2, f"从 R1 store 应回放 2 条，实际 {len(replayed)}"
    assert verify_hash_chain(replayed), "回放的链必须可验"
    assert [e.action for e in replayed] == ["tool.low", "tool.critical"]


def test_verifying_audit_port_uses_shared_sink() -> None:
    """VerifyingAuditPort 构造接受 sink，record 后验链仍生效。"""
    store = InMemoryEventStore()
    sink = R1AuditEventSink(store)
    port = VerifyingAuditPort(AuditLogger(sink=sink))
    asyncio.run(port.record({"action": "tool.call", "when": 1}, PortCtx(session_id="s")))
    assert verify_audit_chain(port) is True
    assert len(asyncio.run(replay_audit_events(sink, "s"))) == 1


# ---- FR-3 真实时钟 + seq 对齐 R1 ----
def test_event_envelope_ts_uses_injected_clock() -> None:
    """ts 来自注入 clock 而非 float(n-1)（注入固定时钟断言精确值）。"""
    stream = InMemoryEventStream()
    fixed = 1_700_000_000.5
    bus = bind_stream_projection(stream, clock=lambda: fixed)
    bus.emit(Notice(phase=LifecyclePhase.AFTER_TOOL, session_id="s", port="tool",
                    payload={"seq": 7}))
    events = _drain(stream, STREAM_TOPIC)
    assert len(events) == 1
    assert events[0].ts == fixed, f"ts 应为注入时钟 {fixed}，实际 {events[0].ts}"
    assert events[0].seq == 7, f"seq 应来自 Notice.payload 的 R1 事件序号，实际 {events[0].seq}"


def test_notice_carries_seq_field() -> None:
    """Notice 有可选 seq 字段（组合层 DTO，不下沉 R1），runner_notifier 透传。"""
    n = Notice(phase=LifecyclePhase.AFTER_TOOL, session_id="s", seq=11)
    assert n.seq == 11
    bus_holder: list[Notice] = []
    from compose import LifecycleBus
    bus = LifecycleBus()
    bus.on(LifecyclePhase.AFTER_TOOL, bus_holder.append, name="capture")
    notify = runner_notifier(bus)
    notify("after_tool", {"session_id": "s", "seq": 13})
    assert bus_holder and bus_holder[0].seq == 13, "runner_notifier 应透传 seq"


# ---- FR-4 生产 CRITICAL 沙盒装配 ----
def test_production_critical_executes_via_security_capability() -> None:
    """生产装配从 SecurityCapability 取 sandbox/human/audit，CRITICAL 审批后真执行。"""
    sec = SecurityCapability(
        sandbox=LocalSandbox(allow_sync=True),
        human=ConsoleHumanPort(answerer=lambda q, o: "approve"),
    )
    rt = assemble_runtime(_noop_call, security=sec)
    assert rt.tools is not None and rt.tools._sandbox is not None, "生产 registry 必须有 sandbox"
    rt.tools.register(_critical_tool(), lambda: "deployed")
    res = asyncio.run(rt.tools.call(_call(), PortCtx(session_id="s-prod")))
    assert res.ok and res.value == "deployed", f"CRITICAL 审批通过应执行，实际 {res.error}"
    assert len(rt.audit.events()) == 1, "CRITICAL 也应写审计"


def test_no_human_placeholder_removed() -> None:
    """_NoHuman 死占位删除：默认装配用真实 SecurityCapability 默认实现。"""
    import api.runtime as runtime_module
    assert not hasattr(runtime_module, "_NoHuman"), "_NoHuman 应删除"
    rt = assemble_runtime(_noop_call)
    assert rt.tools is not None and rt.tools._sandbox is not None
    assert rt.tools._human is not None


# ---- FR-5 单 bus ----
def test_builder_stream_bus_is_the_runner_bus() -> None:
    """AgentBuilder.stream_bus() 返回的 bus 就是 Runner.notify 用的 bus（消除双 bus）。"""
    arch = _dummy_arch()
    stream = InMemoryEventStream()
    builder = AgentBuilder(arch).project_events(stream)
    rt = builder.runtime(_noop_call)
    assert rt.bus is builder.stream_bus(), "stream_bus getter 与 Runner.notify 必须同一 bus"


def _dummy_arch() -> object:
    from runtime import Effect, Terminate
    from typing import Iterator
    class _Arch:
        name = "dummy"
        def compile(self) -> object:
            def program() -> Iterator[object]:
                yield Terminate(output="done")
            return program
    return _Arch()


# ---- FR-6 审计缺席 fail-closed ----
def test_low_without_audit_fails_closed() -> None:
    """审计缺席 fail-closed：LOW 工具无 audit 时拒绝执行（不再静默降级）。"""
    reg = ToolRegistry()
    reg.register(ToolSpec(name="low", danger=DangerLevel.LOW), lambda: 1)
    res = asyncio.run(reg.call(ToolCall(call_id="c", name="low", args={}), PortCtx()))
    assert not res.ok and res.error == "NO_AUDIT", f"应 fail-closed，实际 {res.ok}/{res.error}"


# ---- FR-7 trigger_danger 生效 ----
def test_trigger_danger_honors_non_critical_level() -> None:
    """ApprovingHumanPort.ask 透传 danger，trigger_danger=HIGH 时 HIGH 触发审批。"""
    flow = ApprovalFlow(trigger_danger="HIGH", timeout=1)
    inner = _ApproveHuman()
    port = ApprovingHumanPort(inner, flow)
    ctx = PortCtx()
    # HIGH 触发（若 danger 未透传，默认 CRITICAL 会 deny 且不问人）
    out = asyncio.run(port.ask("q", ["approve", "deny"], ctx, danger=DangerLevel.HIGH))
    assert out == "approve" and len(inner.calls) == 1, "trigger_danger=HIGH 应触发审批"
    # CRITICAL 不触发（trigger 是 HIGH）——配置生效的反向证明
    out2 = asyncio.run(port.ask("q2", ["approve", "deny"], ctx, danger=DangerLevel.CRITICAL))
    assert out2 == "deny" and len(inner.calls) == 1, "非触发级别应 deny 且不问人"


# ---- FR-8 install 两参协议 ----
def test_tools_capability_install_two_arg_protocol() -> None:
    """ToolsCapability.install(container, reg) 对齐 Capability 协议。"""
    cap = ToolsCapability()
    reg = Registry()
    cap.install(object(), reg)  # 两参；旧单参签名会 TypeError
    assert reg.has("tool")


# ---- FR-9 单 store ----
def test_audit_sink_and_checkpoint_share_one_r1_store() -> None:
    """audit sink 与 checkpoint 共享同一 R1 EventStore 实例（消除双写两套 store）。"""
    shared = InMemoryEventStore()
    mgr = CheckpointManager(store=shared)
    store = CheckpointEventStore(mgr, inner=shared)
    sink = R1AuditEventSink(shared)
    assert sink.store is shared and store.inner is shared, "两者必须共享同一 store 实例"
    # 双写消除：mgr 自己不再另建 store
    assert mgr.store is shared, "mgr 应复用注入的 store"


# ---- 旧断言清理的回归保障 ----
def test_safe_tools_need_no_audit() -> None:
    """SAFE 工具仍直执行，不需要 audit（fail-closed 只针对 LOW 及以上）。"""
    reg = ToolRegistry()
    reg.register(ToolSpec(name="safe", danger=DangerLevel.SAFE), lambda: "ok")
    res = asyncio.run(reg.call(ToolCall(call_id="c", name="safe", args={}), PortCtx()))
    assert res.ok and res.value == "ok"
