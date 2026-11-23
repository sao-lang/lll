"""A1 行为门禁：注入的 audit 真进执行链（不是影子对象）。

用真实门面装配出的 AgentRuntime.tools（生产同一 ToolRegistry）直接调用 LOW 工具，
断言注入的 AuditPort 收到审计记录。绕开 LLM/ReAct 测试脚手架噪声，
但走的是真装配链（use → _arun → assemble_runtime → ToolRegistry）。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, PortCtx, ToolCall, ToolSpec
from harness.model import ModelCapability
from harness.security import SecurityCapability
from harness.tools import clear_pending, tool


class _SpyAudit:
    """履约 AuditPort：async record + bind_sink。"""
    name = "spy_audit"
    records: list[dict] = []
    sinks: list[object] = []

    def __init__(self) -> None:
        from harness.security import AuditLogger
        self._inner = AuditLogger()

    def bind_sink(self, sink: object) -> None:
        self.sinks.append(sink)

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


@tool(name="gate_audit_tool", description="门禁审计工具", danger=DangerLevel.LOW)
def _low_tool(x: str = "v") -> str:
    """LOW 工具：先记审计再直执行。"""
    return x


def test_gate_a1_injected_audit_receives_real_tool_event() -> None:
    """注入的 audit 必须在真工具调用后收到审计记录（不是被新建对象替代）。"""
    clear_pending()
    audit = _SpyAudit()
    agent = Agent(ReAct([_low_tool], llm="fake"), session_id="gate-a1")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(SecurityCapability(audit=audit))  # type: ignore[arg-type]
    agent.register_tool("gate_audit_tool", _low_tool, danger=DangerLevel.LOW)

    # 真实装配（use → assemble_runtime），拿到生产 ToolRegistry
    import asyncio
    asyncio.run(agent._arun())
    rt = agent.runtime
    assert rt is not None and rt.tools is not None

    result = asyncio.run(rt.tools.call(ToolCall(call_id="gate-1", name="gate_audit_tool",
                                                  args={"x": "v"}),
                                       PortCtx(session_id="gate-a1")))
    assert result.ok, f"工具调用失败：{result.error}"
    assert audit.records, "注入的 audit 未收到审计记录（影子对象）"
    assert audit.sinks, "注入的 audit 未绑定同源 sink"
