# purity-exempt-file: 导出 B.1.2 指定默认实现 InMemoryEventStream 的名字
"""R10 可观测（observability）：Trace + TelemetryPort/EventStreamPort 默认实现。

依赖只向下：compose + runtime。复用 R1 Event 为权威事件源，EventEnvelope 是其
投影（两者不可混用）。采集以 Lifecycle 插件形态接入，通知型不拦截。
"""
from __future__ import annotations

from .types import EventEnvelope, ReadModel, TraceSpan
from .telemetry import NoopTelemetry, Trace
from .stream import InMemoryEventStream, Projection, TimelineProjection

__all__ = [
    "TraceSpan", "EventEnvelope", "ReadModel",
    "NoopTelemetry", "Trace",
    "InMemoryEventStream", "Projection", "TimelineProjection",
]
