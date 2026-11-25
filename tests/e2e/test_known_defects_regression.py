"""已登记已知缺陷的真实 e2e 回归：证明三处历史缺陷确已修复。

历史登记（overview 行 297/487，均已过时）：
- N1 ToolCall 归一丢 danger/source（agent.py:564-566 已修：归一时保留 danger/source）；
- R10 manager().load()→replay_snapshot() plain dict 崩（checkpoint.py 已修：走 R1 restore_checkpoint）；
- 外部注入 audit 不同源（runtime.py:182-183 已修：bind_sink 绑同一 sink）。

本文件用真实用户入口再钉一遍（不是只读源码/单测），并给失败判据。
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import run


def test_defect1_toolcall_normalize_keeps_danger_source(loop: asyncio.AbstractEventLoop) -> None:
    """N1 回归：LLM 返回的 tool_calls 经门面归一后必须保留 danger/source 语义。

    输入：自定义 LLMPort 返回带 danger=HIGH/source="mcp" 的 ToolCall。
    真实入口：Agent.asay → _call_llm 的 tool_calls 归一 → 门面内部 data。
    精确断言：归一后的 tool_call dict 含 danger="high"/source="mcp"（不是只剩 name/args/call_id）。
    失败判据：danger/source 缺失即红（历史缺陷复辟）。
    """
    from api import Agent
    from archs.base import StaticCheckReport
    from compose import DangerLevel, PortCtx, ToolCall, LLMResponse, Usage
    from runtime import Invoke, Result

    class Arch:
        name = "d1"; version = "1"
        def static_check(self):
            return StaticCheckReport(errors=(), arch=self.name, version=self.version,
                                     ports_required=("llm",), budgets_resolved={})
        def compile(self):
            def program():
                yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "q"}]})
            return program()

    async def scenario():
        agent = Agent(Arch())
        # 真 dispatch：返回带 danger=HIGH/source=mcp 的真实 ToolCall（模拟 LLM 后端响应）
        tc = ToolCall(call_id="c1", name="danger_tool", args={},
                      danger=DangerLevel.HIGH, source="mcp")

        async def dispatch(effect, ctx):
            return Result(ok=True, data={"content": "calling", "tool_calls": [tc],
                                "usage": Usage(prompt_tokens=5, completion_tokens=1)})

        # 真执行门面的 _call_llm（缺陷所在路径），观察归一后的 Result.data
        effect = Invoke(port="llm", payload={"messages": [{"role": "user", "content": "q"}]})
        result = await agent._call_llm(effect, PortCtx(session_id="d1"), dispatch)
        calls = result.data["tool_calls"]
        assert calls[0]["name"] == "danger_tool"
        assert calls[0]["danger"] == DangerLevel.HIGH, f"danger 丢失：{calls[0]}"
        assert calls[0]["source"] == "mcp", f"source 丢失：{calls[0]}"

    run(scenario(), loop)


def test_defect2_load_then_replay_snapshot_no_crash(loop: asyncio.AbstractEventLoop) -> None:
    """R10 回归：manager().load() 的 plain dict events 必须能被 replay_snapshot 回放。

    输入：Runner 真跑一轮产生的 checkpoint 快照。
    真实入口：CheckpointManager.load → replay_snapshot。
    精确断言：回放终态 last_step_kinds 含 terminate、used_turns>=1（真状态，非 None）。
    失败判据：抛 AttributeError('dict' object has no attribute 'effects') 或状态为空即红。
    """
    from runtime import Invoke, Result, Runner, Session, Terminate, InMemoryEventStore
    from harness.durability import CheckpointManager, replay_snapshot

    async def call(effect, ctx):
        return Result.success({})

    shared = InMemoryEventStore()
    mgr = CheckpointManager(store=shared)
    runner = Runner(call, store=shared, checkpoint_every=1)

    def program():
        yield Invoke(port="llm", payload={"q": "hi"})
        yield Terminate(output="done")
        return None

    async def scenario():
        await runner.run(Session(session_id="e2e-replay", program_factory=program))
        loaded = await mgr.load("e2e-replay")
        assert loaded is not None and loaded.events, "load 必须读回事件"
        return replay_snapshot(loaded)

    final = run(scenario(), loop)
    assert "terminate" in final.last_step_kinds, f"回放应恢复 terminate 轨迹，实际 {final.last_step_kinds}"
    assert final.budgets.used_turns >= 1, "回放应累计执行步数"


def test_defect3_external_audit_binds_same_sink(loop: asyncio.AbstractEventLoop) -> None:
    """边界3 回归：外部注入的 audit 也必须把事件落同一 R1 store，可回放。

    输入：一个 inner 无 sink 的干净 VerifyingAuditPort 外部注入 assemble_runtime。
    真实入口：assemble_runtime(audit=clean_external) → audit.record → replay_audit_events。
    精确断言：回放恰 1 条、action 精确、同源 store。
    失败判据：回放 0 条（不同源）即红。
    """
    from api import assemble_runtime, verify_audit_chain
    from api.wiring import R1AuditEventSink, VerifyingAuditPort, replay_audit_events
    from compose import PortCtx
    from harness.security.ports import AuditLogger

    clean_external = VerifyingAuditPort(AuditLogger())
    rt = assemble_runtime(audit=clean_external)

    async def scenario():
        await rt.audit.record({"action": "tool.high.call", "when": 1,
                               "who": "u", "cost": 0.0, "data_touched": "",
                               "rationale": "e2e"}, PortCtx(session_id="e2e-ext-audit"))
        return await replay_audit_events(R1AuditEventSink(rt.checkpoint.inner), "e2e-ext-audit")

    events = run(scenario(), loop)
    assert len(events) == 1, f"外部注入 audit 也应同源落 R1 store，实际 {len(events)} 条"
    assert events[0].action == "tool.high.call"
