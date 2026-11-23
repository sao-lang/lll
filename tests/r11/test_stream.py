"""R11-T04 Q2 事件流/Q3 Projection 外接：只读+续读（TDD RED → GREEN）。"""
from __future__ import annotations

import asyncio

from api import StreamGateway
from harness.observability import EventEnvelope, InMemoryEventStream, TimelineProjection


def _drain(stream, topic, since=None):
    async def go():
        return [e async for e in stream.subscribe(topic, since=since)]
    return asyncio.run(go())


def test_stream_gateway_publish_subscribe() -> None:
    """外接系统经 StreamGateway 发布/订阅，不 import 内核内部。"""
    gw = StreamGateway()
    gw.publish(EventEnvelope(seq=1, topic="trace.span", ts=1.0,
                             type="span", data={"name": "llm"}))
    assert [e.seq for e in _drain(gw.stream(), "trace.span")] == [1]


def test_stream_resume_from_seq() -> None:
    """从 seq 续读（at-least-once 去重由消费方）。"""
    gw = StreamGateway()
    for i in (1, 2, 3):
        gw.publish(EventEnvelope(seq=i, topic="tool.call", ts=float(i),
                                 type="call", data={"i": i}))
    assert [e.seq for e in _drain(gw.stream(), "tool.call", since=1)] == [2, 3]


def test_projection_readonly() -> None:
    """Projection 只读投影，不写内核事件日志。"""
    gw = StreamGateway()
    gw.publish(EventEnvelope(seq=1, topic="trace.span", ts=1.0,
                             type="span", data={"name": "llm"}))
    model = gw.project(TimelineProjection(), "trace.span")
    assert model.name == "timeline" and model.data["count"] == 1
