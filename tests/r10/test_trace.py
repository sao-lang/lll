"""R10-T01 全链路 trace：一个 trace_id 串联 + 组件名留痕（TDD RED → GREEN）。"""
from __future__ import annotations

from compose import PortCtx
from harness.observability import NoopTelemetry, Trace, TraceSpan


def _ctx() -> PortCtx:
    return PortCtx(session_id="s10", run_id="r10", trace_span="root-1")


def test_t01_span_nesting_parent_child() -> None:
    """trace 用同一 trace_id 串联，span 有 parent_id，组件 name 留痕。"""
    trace = Trace(trace_id="t-1", telemetry=NoopTelemetry())
    with trace.span("model.call", component="model") as parent:
        with trace.span("tool.call", component="tools") as child:
            parent_id = parent.span_id
            child_id = child.span_id
    spans = trace.spans()
    assert len(spans) == 2
    assert all(s.trace_id == "t-1" for s in spans)
    assert spans[0].span_id == parent_id
    assert spans[1].parent_id == parent_id
    assert spans[1].name == "tool.call" and spans[1].component == "tools"


def test_t01_metric_and_log() -> None:
    """metric/log 经 TelemetryPort 记录，故障不影响主流程。"""
    telemetry = NoopTelemetry()
    trace = Trace(trace_id="t-2", telemetry=telemetry)
    trace.record_metric("tokens", 42.0, tags={"model": "m1"})
    trace.record_log({"event": "x"})
    assert len(telemetry.metrics()) == 1
    assert telemetry.metrics()[0][1] == 42.0
    assert len(telemetry.logs()) == 1
