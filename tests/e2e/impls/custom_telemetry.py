"""用户侧自定义遥测 SDK（e2e 装填验证用，src 零改）。

履约 compose.ports.TelemetryPort 的 span/metric/log，并实现框架 Trace 依赖的
私有登记契约 _register/_replace。与 NoopTelemetry 的区别：公开登记/替换调用
计数，供 e2e 断言"用户注入的 SDK 被真装填"而非只是构造了一个死对象。

反证变异点（TRACE-01）：把 _register 改坏（如直接 return 不 append / 不计数），
spans() 将为空且 register_calls == 0，用例立即变红。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator

from compose import PortCtx
from harness.observability import TraceSpan


class CustomTelemetry:
    """可注入 ObservabilityCapability 的用户自定义遥测后端。"""

    name = "custom_telemetry"

    def __init__(self) -> None:
        self.register_calls = 0
        self.replace_calls = 0
        self._spans: list[TraceSpan] = []
        self._metrics: list[tuple[str, float, dict[str, Any] | None]] = []
        self._logs: list[dict[str, Any]] = []

    # ---------- 框架 Trace 私有契约 ----------
    def _register(self, span: TraceSpan) -> None:
        """span 开始：登记并计数。"""
        self.register_calls += 1
        self._spans.append(span)

    def _replace(self, ended: TraceSpan) -> None:
        """span 结束：用结束态替换同 span_id 记录并计数。"""
        self.replace_calls += 1
        self._spans = [ended if s.span_id == ended.span_id else s for s in self._spans]

    # ---------- TelemetryPort 履约 ----------
    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[TraceSpan]:  # type: ignore[override]
        """手动 span：进入登记、退出替换（与 NoopTelemetry 同语义）。"""
        span = TraceSpan(trace_id=str(ctx.trace_span or "root"), span_id=name, name=name)
        started = time.perf_counter()
        self._register(span)
        try:
            yield span
        finally:
            self._replace(replace(span, duration_ms=(time.perf_counter() - started) * 1000.0))

    def metric(self, name: str, value: float,
               tags: dict[str, Any] | None = None) -> None:
        """指标：内存留痕。"""
        self._metrics.append((name, value, dict(tags) if tags else None))

    def log(self, event: dict[str, Any]) -> None:
        """日志：内存留痕。"""
        self._logs.append(dict(event))

    # ---------- 只读快照 ----------
    def spans(self) -> tuple[TraceSpan, ...]:
        return tuple(self._spans)

    def metrics(self) -> tuple[tuple[str, float, dict[str, Any] | None], ...]:
        return tuple(self._metrics)

    def logs(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._logs)
