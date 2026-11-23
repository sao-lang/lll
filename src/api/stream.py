# purity-exempt-file: InMemoryEventStream 为 R10 B.1.2 指定默认实现命名，生产级有界队列真实实现
"""R11 Q2/Q3 外接契约：StreamGateway（只经契约接，不 import 内核内部/写事件日志）。"""
from __future__ import annotations

from collections.abc import Sequence

from harness.observability import EventEnvelope, InMemoryEventStream, ReadModel
from harness.observability.stream import Projection

__all__ = ["StreamGateway"]


class StreamGateway:
    """事件流网关：发布/订阅/投影只读，消费方不碰 R1 事件日志。"""
    name = "stream_gateway"

    def __init__(self, stream: InMemoryEventStream | None = None) -> None:
        """构造网关（默认 InMemoryEventStream，可注入外部实现）。"""
        self._stream = stream or InMemoryEventStream()

    def publish(self, event: EventEnvelope) -> None:
        """发布一个只读信封（外部系统经此接，不直接 append 内核事件）。"""
        self._stream.publish(event)

    def stream(self) -> InMemoryEventStream:
        """返回只读订阅视图（实际是 AsyncIterator 提供者，消费方不写）。"""
        return self._stream

    def project(self, projection: Projection, topic: str, since: int | None = None) -> ReadModel:
        """按 topic 投影读模型（同步快照，异步流由调用方 drain）。"""
        import asyncio

        async def drain() -> Sequence[EventEnvelope]:
            return [e async for e in self._stream.subscribe(topic, since=since)]
        events = asyncio.run(drain())
        return projection.project(events)
