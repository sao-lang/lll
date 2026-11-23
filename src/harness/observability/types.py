"""R10 可观测 DTO（R10 §4/§5）：TraceSpan / EventEnvelope / ReadModel。

命名纪律（R10 §4.1）：本 EventEnvelope 与 R4 StreamEvent（LLM 流式 token）形态与
用途不同，不可同名；事件流侧一律用 EventEnvelope。trace 不下沉 R1 内核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from runtime import DynamicValue

__all__ = ["TraceSpan", "EventEnvelope", "ReadModel"]


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """链路 span：trace_id/span_id/parent_id/name/component + 耗时/token/cost。"""
    trace_id: str
    span_id: str
    name: str
    parent_id: str = ""
    component: str = ""
    duration_ms: float = 0.0
    tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """事件流通用信封（R1 append-only event log 的只读投影）。

    字段：
        seq: 全局单调序号（since 续读）。topic: session/effect/trace/audit/metric。
        ts: 注入时钟（墙钟由适配器决定，内核不隐式取）。type: 事件类型。
        data: 载荷形态由 topic 决定（规则 3 动态边界）。
    """
    seq: int
    topic: str
    ts: float
    type: str
    data: DynamicValue


@dataclass(frozen=True, slots=True)
class ReadModel:
    """投影读模型：name + data（可从事件完全重建）。"""
    name: str
    data: dict[str, DynamicValue] = field(default_factory=dict)
