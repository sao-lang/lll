"""Blocker1 RED：注入 audit 必须保留验链（fail-closed 不被绕过）。

注入自定义 AuditPort → 执行链的 audit 必须是 VerifyingAuditPort（或其等价验链），
每条 record 后验链；篡改事件必须抛 ValueError。
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import ReAct
from compose import DangerLevel, PortCtx, ToolCall, ToolSpec
from harness.model import ModelCapability
from harness.security import SecurityCapability
from harness.tools import clear_pending, tool


class _SpyAudit:
    """注入的自定义 AuditPort：内部用真实 AuditLogger 保证哈希链字段完整。"""
    name = "spy_audit"
    records: list[dict] = []

    def __init__(self) -> None:
        from harness.security import AuditLogger
        self._inner = AuditLogger()
        self.sink_bound = False

    def bind_sink(self, sink: object) -> None:
        self.sink_bound = True  # 注入审计应被绑定同源 sink

    async def record(self, event: dict, ctx: object) -> None:
        self.records.append(event)
        await self._inner.record(event, ctx)

    def events(self) -> tuple[object, ...]:
        return self._inner.events()


class _LLM:
    name = "fake"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@tool(name="gate_verified_tool", description="验链审计工具", danger=DangerLevel.LOW)
def _low() -> str:
    return "ok"


def test_blocker1_injected_audit_keeps_chain_verification() -> None:
    clear_pending()
    audit = _SpyAudit()
    agent = Agent(ReAct([], llm="fake"), session_id="gate-b1-audit")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(SecurityCapability(audit=audit))  # type: ignore[arg-type]
    agent.register_tool("gate_verified_tool", _low, danger=DangerLevel.LOW)

    asyncio.run(agent._arun())
    rt = agent.runtime
    assert rt is not None and rt.tools is not None
    result = asyncio.run(rt.tools.call(
        ToolCall(call_id="c1", name="gate_verified_tool", args={}),
        PortCtx(session_id="gate-b1-audit")))
    assert result.ok

    # 核心：执行链拿到的必须是验链包装（不是裸注入对象）
    active = rt.tools._audit
    assert active is not None
    assert audit.records, "注入 audit 未收到记录"
    assert audit.sink_bound, "注入 audit 未绑定同源 sink"
    assert hasattr(active, "verify_external"), (
        f"注入 audit 绕过了验链：执行链拿到 {type(active).__name__}")
