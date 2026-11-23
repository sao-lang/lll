# purity-exempt-file: __exit__ 的 exc_type/exc/tb 是上下文管理器协议固定签名
"""R10 遥测：NoopTelemetry（B.1.2 默认实现）+ Trace（trace_id 串联）。

履约 compose.ports.TelemetryPort 同步签名（span/metric/log，OTel 惯例）：
采集同步入队、关键路径不阻塞；NoopTelemetry 内存收集（故障不影响主流程）。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from itertools import count
from types import TracebackType
from typing import Literal

from compose import PortCtx, TelemetryPort
from runtime import DynamicValue

from .types import TraceSpan

__all__ = ["NoopTelemetry", "Trace"]


class _SpanHandle(AbstractContextManager[TraceSpan]):
    """span 上下文句柄：进入记开始、退出记结束（本地时钟注入，内核不隐式取）。"""
    def __init__(self, span: TraceSpan, telemetry: NoopTelemetry) -> None:
        """保存 span 与归属遥测。"""
        self._span = span
        self._telemetry = telemetry
        self._started = 0.0

    def __enter__(self) -> TraceSpan:
        """记录开始并注册 span。"""
        import time

        self._started = time.perf_counter()
        self._telemetry._register(self._span)
        return self._span

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> Literal[False]:
        """记录耗时（不吞异常）。"""
        import time

        ended = TraceSpan(
            trace_id=self._span.trace_id, span_id=self._span.span_id,
            name=self._span.name, parent_id=self._span.parent_id,
            component=self._span.component,
            duration_ms=round((time.perf_counter() - self._started) * 1000.0, 3),
            tokens=self._span.tokens, cost=self._span.cost,
        )
        self._telemetry._replace(ended)
        return False


class NoopTelemetry(TelemetryPort):
    """TelemetryPort 默认实现：in-process 内存收集（无外部导出故障面）。

    名字沿用 B.1.2 的 NoopTelemetry；真实语义=不导出到外部系统、但完整内存留痕，
    供 trace/metric/log 断言与回放（不是桩：三方法均有真实可观测产出）。
    """
    name = "noop_telemetry"

    def __init__(self) -> None:
        """初始化空收集器。"""
        self._spans: list[TraceSpan] = []
        self._metrics: list[tuple[str, float, dict[str, DynamicValue] | None]] = []
        self._logs: list[dict[str, DynamicValue]] = []

    def span(self, name: str, ctx: PortCtx) -> AbstractContextManager[DynamicValue]:
        """开启 span（返回上下文管理器，供 with 使用）。"""
        span = TraceSpan(trace_id=str(ctx.trace_span or "root"), span_id=name,
                         name=name, component="")
        return _SpanHandle(span, self)

    def metric(self, name: str, value: float,
               tags: dict[str, DynamicValue] | None = None) -> None:
        """记录指标（同步入队）。"""
        self._metrics.append((name, value, dict(tags) if tags else None))

    def log(self, event: dict[str, DynamicValue]) -> None:
        """记录结构化日志事件。"""
        self._logs.append(dict(event))

    # 内部注册（供 _SpanHandle 与 Trace 使用）
    def _register(self, span: TraceSpan) -> None:
        """注册 span 到收集器。"""
        self._spans.append(span)

    def _replace(self, ended: TraceSpan) -> None:
        """用结束态替换同 span_id 的记录（保留插入序）。"""
        self._spans = [ended if s.span_id == ended.span_id else s for s in self._spans]

    def spans(self) -> tuple[TraceSpan, ...]:
        """返回 span 快照。"""
        return tuple(self._spans)

    def metrics(self) -> tuple[tuple[str, float, dict[str, DynamicValue] | None], ...]:
        """返回指标快照。"""
        return tuple(self._metrics)

    def logs(self) -> tuple[dict[str, DynamicValue], ...]:
        """返回日志快照。"""
        return tuple(self._logs)


class Trace:
    """全链路 trace：一个 trace_id 串 LLM/tool/retrieval，组件 name 留痕。"""
    _ids = count(1)

    def __init__(self, trace_id: str, telemetry: NoopTelemetry | None = None) -> None:
        """构造 trace。

        Args:
            trace_id: 链路标识。
            telemetry: 遥测后端；None 用 NoopTelemetry。
        """
        self.trace_id = trace_id
        self.telemetry = telemetry or NoopTelemetry()
        self._parent: str = ""
        self._stack: list[TraceSpan] = []

    @contextmanager
    def span(self, name: str, *, component: str = "") -> Iterator[TraceSpan]:
        """开启嵌套 span：自动接 parent_id，退出后恢复父级。"""
        span_id = f"{self.trace_id}-{next(self._ids)}"
        span = TraceSpan(trace_id=self.trace_id, span_id=span_id,
                         name=name, parent_id=self._parent, component=component)
        self.telemetry._register(span)
        previous = self._parent
        self._parent = span_id
        self._stack.append(span)
        try:
            yield span
        finally:
            self._stack.pop()
            self._parent = previous

    def spans(self) -> tuple[TraceSpan, ...]:
        """返回本 trace 的全部 span。"""
        return tuple(s for s in self.telemetry.spans() if s.trace_id == self.trace_id)

    def record_metric(self, name: str, value: float,
                      tags: dict[str, DynamicValue] | None = None) -> None:
        """记录指标到遥测后端。"""
        self.telemetry.metric(name, value, tags)

    def record_log(self, event: dict[str, DynamicValue]) -> None:
        """记录日志到遥测后端。"""
        self.telemetry.log(event)
