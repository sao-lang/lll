# purity-exempt-file: InMemoryEventStream 为 B.1.2 指定默认实现命名，生产级有界队列真实实现
"""R10 事件流：InMemoryEventStream（B.1.2 默认实现）+ Projection。

投递语义（R10 §4.1）：只读投影 / at-least-once（按 seq 去重由消费方承担）/
从 seq 续读 / 有界队列满丢最旧并标 event_dropped / topic 命名分层。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from compose import EventStreamPort
from runtime import DynamicValue

from .types import EventEnvelope, ReadModel

__all__ = ["InMemoryEventStream", "Projection", "TimelineProjection"]

_DEFAULT_CAPACITY = 1000
_DROPPED_TOPIC = "trace.event_dropped"


class InMemoryEventStream(EventStreamPort):
    """EventStreamPort 默认实现：进程内按 topic 分流的只读事件流。"""
    name = "in_memory_event_stream"

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        """构造事件流。

        Args:
            capacity: 每 topic 有界队列容量；满时丢最旧并记 dropped。
        """
        self.capacity = capacity
        self._topics: dict[str, list[EventEnvelope]] = {}
        self._dropped = 0

    def publish(self, event: EventEnvelope) -> None:
        """发布事件到其 topic（发布端做 R1 Event → EventEnvelope 转换的落点）。"""
        bucket = self._topics.setdefault(event.topic, [])
        if len(bucket) >= self.capacity:
            bucket.pop(0)
            self._dropped += 1
            dropped = EventEnvelope(seq=event.seq, topic=_DROPPED_TOPIC, ts=event.ts,
                                    type="event_dropped", data={"dropped": 1})
            self._topics.setdefault(_DROPPED_TOPIC, []).append(dropped)
        bucket.append(event)

    def subscribe(self, topic: str, since: int | None = None) -> AsyncIterator[DynamicValue]:
        """订阅 topic，从 since 序号之后开始（只读，不侵入内核）。"""
        return self._iterate(topic, since)

    async def _iterate(self, topic: str, since: int | None) -> AsyncIterator[DynamicValue]:
        """异步迭代器实现（惰性，逐条产出现有事件快照）。"""
        for event in list(self._topics.get(topic, ())):
            if since is None or event.seq > since:
                yield event

    def topics(self) -> list[str]:
        """返回可订阅 topic 列表。"""
        return sorted(t for t in self._topics if t != _DROPPED_TOPIC)

    def dropped_count(self) -> int:
        """返回因背压丢弃的事件数。"""
        return self._dropped


class Projection:
    """投影协议：name + project(events) -> ReadModel（可从事件完全重建）。"""
    name = "projection"

    def project(self, events: Iterable[EventEnvelope]) -> ReadModel:
        """从事件投影读模型（基类返回计数，子类覆写具体语义）。"""
        items = tuple(events)
        return ReadModel(name=self.name, data={"count": len(items)})


class TimelineProjection(Projection):
    """时间线投影：统计事件数（确定性，可从事件重建）。"""
    name = "timeline"

    def project(self, events: Iterable[EventEnvelope]) -> ReadModel:
        """按事件序列产出时间线读模型。"""
        items = tuple(events)
        return ReadModel(name=self.name, data={"count": len(items),
                                                "seqs": [e.seq for e in items]})
