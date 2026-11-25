"""OTLP/HTTP telemetry 后端（examples 层，src 零改）。

履约框架 TelemetryPort（span/metric/log）+ 私有 _register/_replace 契约，
在 span 结束时把 TraceSpan 转成 OTLP/HTTP JSON（POST <endpoint>/v1/traces）
上报 Jaeger 等 OTLP 平台。用 httpx（框架已有依赖），不引入 opentelemetry SDK。

设计要点（契约 2026-11-24/otlp-jaeger）：
- span 真值来自框架 TraceSpan（name/component/duration_ms/tokens/cost），不编造；
- 通知语义：上报失败内部消化 + 计数（export_failures），不抛穿聊天主流程；
- trace_id/span_id 是框架内部字符串 → 稳定 hash 成 OTLP 合法 hex（16/32 位）；
- metric/log 本轮内部留痕（诚实边界：不上报，traces only）。
"""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from typing import Any, Iterator

import httpx

from compose import PortCtx, TelemetryPort
from harness.observability import TraceSpan

OTLP_TRACES_PATH = "/v1/traces"


def _hex_id(raw: str, width: int) -> str:
    """把框架内部 id 稳定映射成 OTLP 合法 hex（截取/补位，非加密用途）。"""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:width] if width <= len(digest) else digest.ljust(width, "0")


class OTLPTelemetry:
    """OTLP/HTTP 遥测后端：span 结束即上报，失败不抛穿。"""

    name = "otlp_telemetry"

    def __init__(self, endpoint: str, *, client: httpx.Client | None = None,
                 service_name: str = "agentkit-react-chat") -> None:
        """构造：endpoint 为 OTLP 基址（如 http://localhost:4318）。"""
        self.endpoint = endpoint.rstrip("/")
        self._client = client
        self._own_client = client is None
        self._service_name = service_name
        self._spans: list[TraceSpan] = []
        self._metrics: list[tuple[str, float, dict[str, Any] | None]] = []
        self._logs: list[dict[str, Any]] = []
        self._ok = 0
        self._fail = 0

    # ---------- 框架私有契约（Trace._register/_replace 调用）----------
    def _register(self, span: TraceSpan) -> None:
        """span 开始：登记（保留插入序）。"""
        self._spans.append(span)

    def _replace(self, ended: TraceSpan) -> None:
        """span 结束：替换同 id 记录 + 导出到 OTLP（失败内部消化）。"""
        self._spans = [ended if s.span_id == ended.span_id else s for s in self._spans]
        self._export_span(ended)

    # ---------- TelemetryPort 履约（同步 API）----------
    @contextmanager
    def span(self, name: str, ctx: PortCtx) -> Iterator[Any]:  # type: ignore[override]
        """兼容 TelemetryPort.span：开一个手动 span（NoopTelemetry 风格）。"""
        span = TraceSpan(trace_id=str(getattr(ctx, "trace_span", "") or "root"),
                         span_id=name, name=name)
        self._register(span)
        try:
            yield span
        finally:
            from dataclasses import replace
            self._replace(replace(span, duration_ms=0.0))

    def metric(self, name: str, value: float,
               tags: dict[str, Any] | None = None) -> None:
        """指标：本轮内部留痕（traces only，不上报，诚实边界）。"""
        self._metrics.append((name, value, tags))

    def log(self, event: dict[str, Any]) -> None:
        """日志：本轮内部留痕（traces only，不上报，诚实边界）。"""
        self._logs.append(dict(event))

    def spans(self) -> tuple[TraceSpan, ...]:
        return tuple(self._spans)

    def metrics(self) -> tuple[tuple[str, float, dict[str, Any] | None], ...]:
        return tuple(self._metrics)

    def logs(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._logs)

    def export_successes(self) -> int:
        return self._ok

    def export_failures(self) -> int:
        return self._fail

    # ---------- OTLP 导出 ----------
    def export_span(self, span: TraceSpan, *,
                    extra_attrs: dict[str, Any] | None = None,
                    start_ns: int | None = None,
                    end_ns: int | None = None) -> None:
        """公开导出入口：供相位桥等外部生产者复用同一 OTLP 通道。

        与 _replace 的差别：不登记进 self._spans（相位 span 属独立时间线），
        只做上报 + 计数；失败同样内部消化。
        start_ns/end_ns 显式传入时用真实时刻（相位到达时刻），否则用导出时刻。
        """
        self._export_span(span, extra_attrs=extra_attrs,
                          start_ns=start_ns, end_ns=end_ns)

    def _otlp_body(self, span: TraceSpan, *,
                   extra_attrs: dict[str, Any] | None = None,
                   start_ns: int | None = None,
                   end_ns: int | None = None) -> dict[str, Any]:
        """把 TraceSpan 映射成 OTLP/HTTP JSON 的 TracesData。"""
        now_ns = end_ns if end_ns is not None else time.time_ns()
        duration_ns = int(span.duration_ms * 1_000_000)
        start_ns = start_ns if start_ns is not None else (now_ns - duration_ns)
        attrs = [
            {"key": "component", "value": {"stringValue": span.component}},
            {"key": "tokens", "value": {"intValue": str(span.tokens)}},
            {"key": "cost_usd", "value": {"doubleValue": span.cost}},
        ]
        # 额外属性（相位 payload：phase/port/exceeded/step/error 等）
        for key, value in (extra_attrs or {}).items():
            if isinstance(value, bool):
                attrs.append({"key": key, "value": {"boolValue": value}})
            elif isinstance(value, (int, float)):
                attrs.append({"key": key, "value": {
                    "doubleValue" if isinstance(value, float) else "intValue": str(value)}})
            else:
                attrs.append({"key": key, "value": {"stringValue": str(value)}})
        item: dict[str, Any] = {
            "traceId": _hex_id(span.trace_id, 32),
            "spanId": _hex_id(span.span_id, 16),
            "name": span.name,
            "kind": 2,  # SPAN_KIND_INTERNAL
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(now_ns),
            "attributes": attrs,
            # TraceSpan 无成败字段 → status 用 UNSET(0)，不硬编码 OK（诚实）
            "status": {"code": 0},
        }
        if span.parent_id:
            # 父子关系映射为 OTLP parentSpanId —— Jaeger 链路树成立（S5）
            item["parentSpanId"] = _hex_id(span.parent_id, 16)
        return {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": self._service_name}}]},
                "scopeSpans": [{
                    "scope": {"name": "agentkit.trace"},
                    "spans": [item],
                }],
            }],
        }

    def _export_span(self, span: TraceSpan, *,
                     extra_attrs: dict[str, Any] | None = None,
                     start_ns: int | None = None,
                     end_ns: int | None = None) -> None:
        """POST 到 OTLP /v1/traces；任何异常内部消化（通知语义不抛穿）。"""
        client = self._client or httpx.Client(timeout=2.0)
        try:
            resp = client.post(
                self.endpoint + OTLP_TRACES_PATH,
                json=self._otlp_body(span, extra_attrs=extra_attrs,
                                     start_ns=start_ns, end_ns=end_ns),
                headers={"Content-Type": "application/json"},
            )
            if 200 <= resp.status_code < 300:
                self._ok += 1
            else:
                self._fail += 1
        except Exception:  # noqa: BLE001 - 遥测失败绝不阻断业务主流程
            self._fail += 1
        finally:
            if self._own_client and self._client is None:
                client.close()
