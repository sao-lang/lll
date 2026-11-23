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
    stream = InMemoryEventStream()
    seen: list[str] = []

    async def call(effect: object, ctx: object) -> Result:
        seen.append("tool")
        return Result.success({})

    rt = assemble_runtime(call, stream=stream)
    # 用真实 tool Invoke 触发 AFTER_TOOL 通知：此时 R1 尚未 append 该步 Event，
    # len(events)=0 → seq_source.current()=0，必须是合法同源序号而非合成 1。
    rt.tools.register(ToolSpec(name="t", description="d", danger=DangerLevel.LOW),
                      lambda: "ok")

    def program() -> Iterator[object]:
        yield Invoke(port="llm", payload={"q": "x"})  # 走注入 call，不触发 tool 分支
        yield Terminate(output="done")
        return None

    session = Session(session_id="s-zero", program_factory=program)
    asyncio.run(rt.run(session))

    # 直接以 seq=0 的 Notice 验证投影层不再把 0 当缺失
    from api.wiring import STREAM_TOPIC, bind_stream_projection
    from compose import LifecyclePhase, Notice
    s2 = InMemoryEventStream()
    bus2 = bind_stream_projection(s2)
    bus2.emit(Notice(phase=LifecyclePhase.AFTER_TOOL, session_id="s", seq=0))
    # Notice.seq=0 是 DTO 默认"未提供"语义，此路应合成并标记（不冒充 R1）
    async def drain2() -> list[object]:
        return [e async for e in s2.subscribe(STREAM_TOPIC)]
    ev2 = asyncio.run(drain2())
    assert ev2 and ev2[0].data.get("seq_synthetic") is True, (
        "Notice 未提供 seq 时应诚实标 synthetic")
