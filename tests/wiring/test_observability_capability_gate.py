"""B3 行为门禁：ObservabilityCapability 注入 telemetry 真生效。

用户注入 telemetry（履约 TelemetryPort）→ Agent.use() → agent.trace 用注入的
telemetry（span/metric/log 真进 spy），而非硬编码 NoopTelemetry。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import PortCtx, ToolSpec
from harness.model import ModelCapability
from harness.observability.capability import ObservabilityCapability


class _SpyTelemetry:
    name = "spy_telemetry"
    spans: list[str] = []
    metrics: list[tuple[str, float]] = []
    logs: list[dict] = []

    def _register(self, span: object) -> None:
        self.spans.append(str(getattr(span, "name", span)))

    def _replace(self, span: object) -> None:
        pass

    def metric(self, name: str, value: float,
               tags: dict | None = None) -> None:
        self.metrics.append((name, value))

    def log(self, event: dict) -> None:
        self.logs.append(event)


class _LLM:
    name = "fake"
    last_usage = None
    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")
    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_gate_b3_injected_telemetry_receives_spans() -> None:
    spy = _SpyTelemetry()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), session_id="gate-b3")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(ObservabilityCapability(telemetry=spy))  # type: ignore[arg-type]

    agent.say("hi")

    assert spy.spans, "注入的 telemetry 未收到任何 span（Trace 硬编码 Noop）"
