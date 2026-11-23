"""三条遗留边界返修的失败测试（TDD RED → GREEN）。

- 边界3：外部注入 audit 也同源（sink 自动绑定共享 R1 store）
- 边界2：CheckpointManager.load() → replay_snapshot() 不再因 plain dict 崩
"""
from __future__ import annotations

import asyncio

from api import assemble_runtime, VerifyingAuditPort
from api.wiring import R1AuditEventSink, replay_audit_events
from compose import PortCtx
from harness.durability import CheckpointManager, replay_snapshot
from harness.security import AuditLogger
from runtime import AgentState, InMemoryEventStore, Message, checkpoint, Session


# ---- 边界3：外部注入 audit 也同源 ----
def test_external_injected_audit_replays_from_r1_store() -> None:
    """外部注入的 audit 也必须把事件落同一 R1 store，可回放（不再是静默不同源）。"""
    rt = assemble_runtime()
    external = rt.audit  # 默认审计（带 sink）——模拟调用方持有一个审计器后再注入
    # 用另一个真正"干净"的外部审计器（inner 无 sink）走注入路径
    clean_external = VerifyingAuditPort(AuditLogger())
    rt2 = assemble_runtime(audit=clean_external)
    asyncio.run(rt2.audit.record({"action": "tool.high.call", "when": 1},
                                 PortCtx(session_id="ext-audit")))
    events = asyncio.run(replay_audit_events(
        R1AuditEventSink(rt2.checkpoint.inner), "ext-audit"))
    assert len(events) == 1, (
        f"外部注入 audit 也应同源落 R1 store，回放实际 {len(events)} 条")
    assert events[0].action == "tool.high.call"


def test_explicit_store_is_shared_base() -> None:
    """调用方可显式注入共享 R1 EventStore，audit sink 与 checkpoint 都用它。"""
    shared = InMemoryEventStore()
    rt = assemble_runtime(store=shared)
    assert rt.checkpoint.inner is shared, "注入 store 应成为 checkpoint 底座"
    asyncio.run(rt.audit.record({"action": "a", "when": 1}, PortCtx(session_id="s")))
    events = asyncio.run(replay_audit_events(R1AuditEventSink(shared), "s"))
    assert len(events) == 1, "注入 store 也应承载审计同源"


# ---- 边界2：manager().load() → replay_snapshot() 真恢复 ----
def test_manager_load_then_replay_snapshot_restores() -> None:
    """R10 预存缺陷：load() 返回的 plain dict events 必须能被 replay_snapshot 回放。"""
    from runtime import Invoke, Result, Runner, Terminate

    async def call(effect: object, ctx: object) -> Result:
        return Result.success({})

    shared = InMemoryEventStore()
    mgr = CheckpointManager(store=shared)
    runner = Runner(call, store=shared, checkpoint_every=1)

    def program() -> object:
        yield Invoke(port="llm", payload={"q": "hi"})
        yield Terminate(output="done")
        return None

    session = Session(session_id="s-replay", program_factory=program)
    asyncio.run(runner.run(session))  # Runner 已把规范化快照写进 shared store
    assert session.events, "真实 run 应产生事件"

    loaded = asyncio.run(mgr.load("s-replay"))
    assert loaded is not None and loaded.events, "load 应读回事件"
    # 当前崩溃：replay_events 对 plain dict 做 event.effects → AttributeError
    final = replay_snapshot(loaded)
    assert final.last_step_kinds and "terminate" in final.last_step_kinds, (
        f"回放应恢复执行轨迹（last_step_kinds），实际 {final}")
    assert final.budgets.used_turns >= 1, "回放应累计执行步数"
