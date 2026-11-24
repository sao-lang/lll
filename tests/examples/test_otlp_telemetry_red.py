"""FR-1/3/4 RED：OTLP/HTTP telemetry 注入实现。

履约 TelemetryPort（span/metric/log）+ 框架私有 _register/_replace；
span 结束时转 OTLP JSON POST 到 <endpoint>/v1/traces；失败内部消化+计数。
HTTP 用 httpx.MockTransport 断言请求帧（不依赖真实 Jaeger）。

先 RED（类不存在），实现后 GREEN。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from compose import PortCtx
from harness.observability import TraceSpan


def _span(name: str = "llm", *, duration: float = 12.5) -> TraceSpan:
    return TraceSpan(trace_id="trace-A", span_id="span-1", name=name,
                     component="llm", duration_ms=duration, tokens=30, cost=0.01)


def test_red_otlp_telemetry_exports_span_on_replace() -> None:
    """span 结束(_replace) → POST /v1/traces，OTLP JSON 含真实字段。"""
    from examples.react_chat.otlp_telemetry import OTLPTelemetry

    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    tel = OTLPTelemetry(endpoint="http://jaeger:4318",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    tel._register(_span())
    tel._replace(_span())

    assert len(captured) == 1, f"未上报：{captured}"
    req = captured[0]
    assert req.method == "POST" and str(req.url).endswith("/v1/traces")
    assert "application/json" in req.headers.get("content-type", "")
    body = json.loads(req.content)
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert spans["name"] == "llm"
    # OTLP 要求 hex id（16/32 位）——框架内部字符串 id 稳定 hash 而来
    assert len(spans["spanId"]) == 16 and int(spans["spanId"], 16) >= 0
    assert len(spans["traceId"]) == 32 and int(spans["traceId"], 16) >= 0
    # OTLP duration 单位是纳秒（整数字符串）
    assert int(spans["endTimeUnixNano"]) - int(spans["startTimeUnixNano"]) == int(12.5 * 1_000_000)
    assert spans["attributes"]  # component/tokens/cost 作为属性上报
    assert spans["status"]["code"] == 0, "无成败字段时 status 应 UNSET(0)，不硬编码 OK"


def test_red_otlp_telemetry_failure_is_swallowed_and_counted() -> None:
    """上报失败（连接拒绝）不抛穿主流程，失败计数可查。"""
    from examples.react_chat.otlp_telemetry import OTLPTelemetry

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tel = OTLPTelemetry(endpoint="http://bad:4318",
                        client=httpx.Client(transport=httpx.MockTransport(boom)))
    tel._register(_span())
    tel._replace(_span())  # 不应抛异常

    assert tel.export_failures() == 1
    assert tel.export_successes() == 0


def test_red_otlp_telemetry_port_contract() -> None:
    """履约 TelemetryPort：span/metric/log 三个同步方法可调用。"""
    from examples.react_chat.otlp_telemetry import OTLPTelemetry
    tel = OTLPTelemetry(endpoint="http://jaeger:4318",
                        client=httpx.Client(transport=httpx.MockTransport(
                            lambda req: httpx.Response(200))))
    # TelemetryPort 非 runtime_checkable → 结构化验证方法存在（协议真意）
    for m in ("span", "metric", "log", "_register", "_replace"):
        assert callable(getattr(tel, m, None)), f"缺契约方法 {m}"
    with tel.span("probe", PortCtx()) as ctx:
        pass
    tel.metric("m", 1.0)
    tel.log({"event": "probe"})
    # metric/log 内部留痕（本轮不上报，诚实边界）
    assert len(tel.metrics()) == 1 and len(tel.logs()) == 1


def test_red_build_agent_wires_otlp_when_endpoint_set(tmp_path: Path, monkeypatch: object) -> None:
    """接线：有 endpoint → ObservabilityCapability 持 OTLP telemetry；无 → Noop。"""
    from examples.react_chat.app import build_agent_async, close_agent
    from harness.observability.capability import ObservabilityCapability
    import asyncio

    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger-test:4318")  # type: ignore[attr-defined]

    async def _run() -> None:
        agent, meta = await build_agent_async("otlp-wire", offline=True)
        try:
            cap = next(c for c in agent._caps.values()
                       if isinstance(c, ObservabilityCapability))
            assert type(cap.telemetry).__name__ == "OTLPTelemetry"
            assert "OTLP" in meta["trace_backend"]
        finally:
            await close_agent(agent)
    asyncio.run(_run())


def test_red_build_agent_defaults_noop_without_endpoint(tmp_path: Path, monkeypatch: object) -> None:
    """无 endpoint → NoopTelemetry，零网络 IO（离线保持）。"""
    from examples.react_chat.app import build_agent_async, close_agent
    from harness.observability.capability import ObservabilityCapability
    import asyncio

    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)  # type: ignore[attr-defined]

    async def _run() -> None:
        agent, meta = await build_agent_async("noop-wire", offline=True)
        try:
            cap = next(c for c in agent._caps.values()
                       if isinstance(c, ObservabilityCapability))
            assert type(cap.telemetry).__name__ == "NoopTelemetry"
            assert "未上报" in meta["trace_backend"]
        finally:
            await close_agent(agent)
    asyncio.run(_run())


def test_red_otlp_maps_parent_span_id() -> None:
    """S5：parent_id 必须映射成 OTLP parentSpanId，Jaeger 链路树才成立。"""
    from examples.react_chat.otlp_telemetry import OTLPTelemetry
    from dataclasses import replace

    captured: list[httpx.Request] = []
    tel = OTLPTelemetry(endpoint="http://jaeger:4318",
                        client=httpx.Client(transport=httpx.MockTransport(
                            lambda req: (captured.append(req), httpx.Response(200))[1])))
    child = replace(_span("tool:tool"), parent_id="parent-span")
    tel._register(child)
    tel._replace(child)
    body = json.loads(captured[0].content)
    span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert "parentSpanId" in span and len(span["parentSpanId"]) == 16
    assert int(span["parentSpanId"], 16) >= 0
