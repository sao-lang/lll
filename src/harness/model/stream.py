"""结构化流式事件帮助函数（R4 §4.1 v1.2）。

把只实现 stream()->AsyncIterator[str] 的旧后端包装成 StreamEvent 流，
使未覆盖 stream_events 的后端也能接入结构化事件通道，且不污染 LLMPort 协议
（避免 runtime_checkable isinstance 因新增方法而拒绝旧实现）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from hooks import LLMRequest, PortCtx, StreamEvent, StreamEventKind

__all__ = ["has_stream_events", "wrap_text_stream"]


def has_stream_events(llm: object) -> bool:
    """后端是否已实现 stream_events()（具名能力探测，非动态 getattr 链）。"""
    # 规则 8 例外：可选能力探测，具名固定字符串，非 ctx.meta/effect.payload 动态键。
    return callable(getattr(llm, "stream_events", None))


def wrap_text_stream(
    llm: object, req: LLMRequest, ctx: PortCtx,
) -> AsyncIterator[StreamEvent]:
    """把文本流后端包成事件流：每块 TEXT_DELTA，末了 USAGE/DONE。

    参数：
        llm: 实现了 stream(req, ctx)->AsyncIterator[str] 且有 last_usage 的后端。
        req: 请求。ctx: 上下文。
    """
    return _wrap(llm, req, ctx)


async def _wrap(
    llm: object, req: LLMRequest, ctx: PortCtx,
) -> AsyncIterator[StreamEvent]:
    # 规则 8 例外：包装具名固定能力的旧后端（stream/last_usage），非动态键读取。
    stream = getattr(llm, "stream")
    last_usage = getattr(llm, "last_usage", None)
    async for chunk in stream(req, ctx):
        yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk)
    if last_usage is not None:
        yield StreamEvent(kind=StreamEventKind.USAGE, data=last_usage)
    yield StreamEvent(kind=StreamEventKind.DONE)
