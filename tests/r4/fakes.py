"""R4 测试专用 fake LLM：录制响应 / 预置流式块。

只在 tests/ 与 demos/ 内使用，不属于框架公共 API。
真实后端（OpenAI-compat 等）由注入方实现 LLMPort 协议提供，框架不内置 fake。
不联网、确定性可回放，保证 R4 测试模块内闭环（CL-2）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from compose import LLMRequest, LLMResponse, PortCtx, StreamEvent, Usage

__all__ = ["RecordingLLM", "StreamLLM", "EventStreamLLM"]


class RecordingLLM:
    """录制式 LLM：按队列返回预置响应，记录调用次数（complete / stream 契约）。

    属性：
        name: 适配器名。call_count: 已调用次数。
        last_usage: 最近一次调用的 usage（供网关流式结算，模拟真后端行为）。
    """

    name = "recording_llm"

    def __init__(self, responses: list[LLMResponse]) -> None:
        """装配预置响应队列。"""
        self._responses = list(responses)
        self._idx = 0
        self.call_count = 0
        self.last_usage: Usage | None = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """返回下一个预置响应；队列用尽时重复最后一个。"""
        self.call_count += 1
        if self._responses:
            resp = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            self.last_usage = resp.usage
            return resp
        return LLMResponse(content="")

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """流式：把最后响应的 content 按字符块产出（缺省空），并暴露 usage 供结算。"""
        content = self._responses[-1].content if self._responses else ""
        self.last_usage = self._responses[-1].usage if self._responses else None
        for ch in content or "":
            yield ch


class StreamLLM:
    """流式 LLM：按预置块逐块产出，验证取消可中断。"""

    name = "stream_llm"

    def __init__(self, chunks: list[str], usage: Usage | None = None) -> None:
        """装配流式块列表与结算用 usage（缺省零成本）。"""
        self._chunks = list(chunks)
        self.last_usage = usage if usage is not None else Usage()

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次性补全：拼接全部块。"""
        return LLMResponse(content="".join(self._chunks), usage=self.last_usage)

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """逐块产出；aclose() 可随时中断（由 async generator 语义保证）。"""
        for chunk in self._chunks:
            yield chunk


class EventStreamLLM:
    """结构化事件流 LLM：直接产出预置 StreamEvent 序列。"""

    name = "event_stream_llm"

    def __init__(self, events: list[StreamEvent]) -> None:
        """装配事件序列。"""
        self._events = list(events)
        self.last_usage = next(
            (e.data for e in self._events if e.kind.value == "usage"), None
        )

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次性补全：拼接所有 TEXT_DELTA。"""
        from compose import StreamEventKind
        text = "".join(
            e.data for e in self._events
            if e.kind is StreamEventKind.TEXT_DELTA and isinstance(e.data, str)
        )
        return LLMResponse(content=text, usage=self.last_usage or Usage())

    def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """文本视图：只产出 TEXT_DELTA 的 data。"""
        from compose import StreamEventKind
        return self._text_stream()

    async def _text_stream(self) -> AsyncIterator[str]:
        from compose import StreamEventKind
        for e in self._events:
            if e.kind is StreamEventKind.TEXT_DELTA and isinstance(e.data, str):
                yield e.data

    def stream_events(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[StreamEvent]:
        """结构化事件流。"""
        return self._events_stream()

    async def _events_stream(self) -> AsyncIterator[StreamEvent]:
        for e in self._events:
            yield e
