"""R10-T02 事件流：订阅 + 从 seq 续读 + 背压丢最旧（TDD RED → GREEN）。"""
from __future__ import annotations

import asyncio

from harness.observability import EventEnvelope, InMemoryEventStream


def _collect(stream, topic: str, since: int | None = None) -> list:
    """异步订阅 → 同步列表（测试夹具，非源码桩）。"""
    async def drain():
        return [e async for e in stream.subscribe(topic, since=since)]
    return asyncio.run(drain())


def test_t02_subscribe_and_resume_from_seq() -> None:
    """订阅 topic 从 since 之后读取；at-least-once 按 seq 去重由消费方承担。"""
    stream = InMemoryEventStream()
    stream.publish(EventEnvelope(seq=1, topic="session.start", ts=1.0,
                                 type="started", data={"id": "s1"}))
    stream.publish(EventEnvelope(seq=2, topic="session.start", ts=2.0,
                                 type="ended", data={"id": "s1"}))
    stream.publish(EventEnvelope(seq=3, topic="tool.call", ts=3.0,
                                 type="called", data={"name": "t"}))
    assert [e.seq for e in _collect(stream, "session.start")] == [1, 2]
    assert [e.seq for e in _collect(stream, "session.start", since=1)] == [2]
    assert set(stream.topics()) == {"session.start", "tool.call"}


def test_t02_bounded_drops_oldest() -> None:
    """有界队列满时丢最旧并标 trace=event_dropped，不阻塞。"""
    stream = InMemoryEventStream(capacity=2)
    for i in range(1, 5):
        stream.publish(EventEnvelope(seq=i, topic="metric.x", ts=float(i),
                                     type="m", data={"i": i}))
    events = _collect(stream, "metric.x")
    assert [e.seq for e in events] == [3, 4]
    assert stream.dropped_count() == 2


def test_t02_projection_read_model() -> None:
    """Projection 从事件重建读模型（可重建性）。"""
    from harness.observability import TimelineProjection

    stream = InMemoryEventStream()
    stream.publish(EventEnvelope(seq=1, topic="trace.span", ts=1.0,
                                 type="span", data={"name": "model"}))
    stream.publish(EventEnvelope(seq=2, topic="trace.span", ts=2.0,
                                 type="span", data={"name": "tool"}))
    projection = TimelineProjection()
    model = projection.project(_collect(stream, "trace.span"))
    assert model.name == "timeline"
    assert model.data["count"] == 2
