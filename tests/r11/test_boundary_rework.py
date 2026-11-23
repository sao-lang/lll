"""独立复审返修 RED→GREEN：三条 Blocker 走真实生产接线的失败测试。

- B-1 checkpoint 共享 store 单次写 + restore_checkpoint 真恢复（不静默坏数据）
- B-2 seq 来自 R1 Event.seq 投影（真实 Runner 发射点携带 seq，非手搓 Notice）
- B-3 生产 Runner 执行 CRITICAL 工具经 dispatch → ToolRegistry 审批+沙盒+审计
"""
from __future__ import annotations

import asyncio
from typing import Iterator

from api import STREAM_TOPIC, assemble_runtime
from api.wiring import CheckpointEventStore
from compose import DangerLevel, PortCtx, ToolSpec
from harness.durability import CheckpointManager
from harness.observability import InMemoryEventStream
from harness.security import AuditLogger, ConsoleHumanPort, LocalSandbox, SecurityCapability
from runtime import (
    AgentState,
    Event,
    InMemoryEventStore,
    Invoke,
    JsonSnapshot,
    Message,
    Result,
    Session,
    Terminate,
)
from runtime.types import restore_state


def _ctx() -> PortCtx:
    return PortCtx(session_id="s-rework", run_id="r-rework")


# ---- B-1：共享 store 单次写 + 真恢复 ----
def test_checkpoint_shared_store_restores_without_corruption() -> None:
    """共享 store 路径 save 后 restore_state 必须真恢复（B-1：不再被 mgr 二次写覆盖）。"""
    shared = InMemoryEventStore()
    mgr = CheckpointManager(store=shared)
    store = CheckpointEventStore(mgr, inner=shared)  # 生产接线的单 store 形态
    state = AgentState(messages=[Message(role="user", content="hello")])
    from runtime.types import _plain, checkpoint

    snap = checkpoint(Session(session_id="s-rework", program_factory=lambda: None,
                              state=state))
    asyncio.run(store.save("s-rework", snap))
    loaded = asyncio.run(store.load("s-rework"))
    assert loaded is not None
    restored = restore_state(loaded["state"])
    assert restored.messages[0].role == "user", (
        f"restore 必须恢复出 Message 而非坏数据，实际 {type(restored.messages[0]).__name__}")
    assert restored.messages[0].content == "hello"


# ---- B-3：生产 Runner 真执行 CRITICAL（dispatch 桥接） ----
def test_production_runner_dispatches_critical_tool() -> None:
    """真实 Runner.run 发 CRITICAL 工具 Invoke → dispatch → registry 审批+沙盒+审计。"""
    class _TrackingSandbox:
        name = "tracking_sandbox"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, fn: object, args: object, ctx: object) -> object:
            self.calls.append("run")
            return fn(**(args if isinstance(args, dict) else {}))  # type: ignore[operator]

    sandbox = _TrackingSandbox()
    sec = SecurityCapability(
        sandbox=sandbox,  # type: ignore[arg-type]
        human=ConsoleHumanPort(answerer=lambda q, o: "approve"),
    )
    stream = InMemoryEventStream()
    # 不注入 call：assemble_runtime 必须用已接线的 registry 构造默认 dispatch
    rt = assemble_runtime(security=sec, stream=stream)
    assert rt.tools is not None
    rt.tools.register(ToolSpec(name="deploy", description="d", danger=DangerLevel.CRITICAL),
                      lambda: "deployed")

    def program() -> Iterator[object]:
        yield Invoke(port="tool", payload={"name": "deploy", "args": {},
                                           "danger": "CRITICAL", "call_id": "crit-1"})
        yield Terminate(output="done")
        return None

    session = Session(session_id="s-crit", program_factory=program)
    result = asyncio.run(rt.run(session))
    assert result.status == "done", f"真实 RUNNER 执行失败: {result.reason}"
    assert len(rt.audit.events()) == 1, (
        f"生产 Runner 执行 CRITICAL 必须写审计，实际 {len(rt.audit.events())} 条")
    assert sandbox.calls, "生产 Runner 执行 CRITICAL 必须进沙盒"


# ---- B-2：真实 Runner 发射的 EventEnvelope seq 来自 R1 Event.seq ----
def test_real_runner_notice_carries_r1_event_seq() -> None:
    """真实 Runner 发出的 notice 经 bind_stream_projection 投影，seq 对应 R1 Event.seq。"""
    stream = InMemoryEventStream()
    seen_ports: list[str] = []

    async def call(effect: object, ctx: object) -> Result:
        seen_ports.append(getattr(effect, "port", ""))
        return Result.success({"ok": True})

    # 注入一个追踪 seq 的 source：R1 每追加一个 Event，seq_source 就能读到
    rt = assemble_runtime(call, stream=stream)

    def program() -> Iterator[object]:
        yield Invoke(port="llm", payload={"q": "hi"})
        yield Terminate(output="done")
        return None

    session = Session(session_id="s-seq", program_factory=program)
    asyncio.run(rt.run(session))

    async def drain() -> list[object]:
        return [e async for e in stream.subscribe(STREAM_TOPIC)]

    events = asyncio.run(drain())
    # R1 内核 Event.seq = 通知时 len(session.events)（runner.py:551 权威定义）。
    # SESSION_END 在事件全部追加后发射，投影 seq 必须 == len(session.events)，
    # 而非进程本地计数 1。当前合成实现给 seq=1（与 R1 的 2 不符）→ RED。
    r1_count = len(session.events)
    assert r1_count > 0, "R1 应产生事件"
    envelope_seqs = [e.seq for e in events]  # type: ignore[union-attr]
    assert r1_count in envelope_seqs, (
        f"EventEnvelope seq {envelope_seqs} 必须含 R1 权威事件数 {r1_count}（同源投影），"
        "当前是进程本地合成计数")
    # 合成标记：同源投影的 seq 不得标 synthetic
    assert not any(e.data.get("seq_synthetic") for e in events), (
        "R1 同源 seq 不应被标 synthetic")


def test_first_event_seq_zero_is_not_treated_as_missing() -> None:
    """R1 首条事件 seq=0 是合法权威序号，不得当"无序号"回退合成计数（SHOULD-1）。"""
    from api.wiring import STREAM_TOPIC, R1SeqTracker, bind_stream_projection
    from compose import LifecyclePhase, Notice
    from harness.observability import EventEnvelope

    # 直接锁住 SHOULD-1 修复点：seq_source.current()==0（R1 首条事件）必须
    # 投影为 seq=0 且 seq_synthetic=False，不得回退到合成计数 1。
    tracker = R1SeqTracker()
    snapshot: JsonSnapshot = {
        "session_id": "s", "version": "1.0", "state": {},
        "events": [{"seq": 0, "session_id": "s"}], "fingerprint": "f",
    }
    tracker.observe(snapshot)  # 无活会话时从 checkpoint 快照读到 R1 Event.seq=0
    assert tracker.current() == 0

    s2 = InMemoryEventStream()
    bus2 = bind_stream_projection(s2, seq_source=tracker)
    bus2.emit(Notice(phase=LifecyclePhase.AFTER_TOOL, session_id="s"))

    async def drain2() -> list[EventEnvelope]:
        return [e async for e in s2.subscribe(STREAM_TOPIC)]

    ev2 = asyncio.run(drain2())
    assert len(ev2) == 1
    assert ev2[0].seq == 0, f"R1 首条事件 seq=0 必须原样投影，实际 {ev2[0].seq}"
    assert ev2[0].data.get("seq_synthetic") is False, "R1 同源 seq=0 不得标 synthetic"

    # 对照：无权威源且 Notice 未提供 seq 时，才诚实合成并标记（不冒充 R1）
    s3 = InMemoryEventStream()
    bind_stream_projection(s3).emit(
        Notice(phase=LifecyclePhase.AFTER_TOOL, session_id="s"))
    async def drain3() -> list[EventEnvelope]:
        return [e async for e in s3.subscribe(STREAM_TOPIC)]
    ev3 = asyncio.run(drain3())
    assert ev3 and ev3[0].data.get("seq_synthetic") is True, (
        "无 R1 序号时应诚实标 synthetic")
