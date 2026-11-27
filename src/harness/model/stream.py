"""结构化流式事件帮助函数。

把只实现 stream()->AsyncIterator[str] 的旧后端包装成 StreamEvent 流，
使未覆盖 stream_events 的后端也能接入结构化事件通道，且不污染 LLMPort 协议
（避免 runtime_checkable isinstance 因新增方法而拒绝旧实现）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from compose import LLMPort, LLMRequest, PortCtx, StreamEvent, StreamEventKind

__all__ = ["has_stream_events", "wrap_text_stream"]


def has_stream_events(llm: LLMPort) -> bool:
    """后端是否已实现 stream_events()（具名能力探测，非动态 getattr 链）。"""
    # 规则 6 例外：可选能力探测，具名固定字符串，非 ctx.meta/effect.payload 动态键。
    return callable(getattr(llm, "stream_events", None))


def wrap_text_stream(
    llm: LLMPort, req: LLMRequest, ctx: PortCtx,
) -> AsyncIterator[StreamEvent]:
    """把文本流后端包成事件流：每块 TEXT_DELTA，末了 USAGE/DONE。

    参数：
        llm: 实现了 stream(req, ctx)->AsyncIterator[str] 且有 last_usage 的后端。
        req: 请求。ctx: 上下文。
    """
    return _wrap(llm, req, ctx)


async def _wrap(
    llm: LLMPort, req: LLMRequest, ctx: PortCtx,
) -> AsyncIterator[StreamEvent]:
    """包装实现：逐块转发 TEXT_DELTA，流末按 last_usage 补 USAGE/DONE。"""
    # 规则 6 例外：包装具名固定能力的旧后端（stream/last_usage），非动态键读取。
    stream = getattr(llm, "stream")
    async for chunk in stream(req, ctx):  # 逐块转发不缓存整段，保持流式低延迟
        yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk)
    # 在流末读取：真实适配器（LLMPort 契约）在流中才写 last_usage，
    # 包装器创建时快照会拿到 None 导致 USAGE 事件缺失（审计 EXP6-B）。
    # 规则 6 例外：具名固定属性探测（LLMPort 契约成员 last_usage），非动态键读取。
    last_usage = getattr(llm, "last_usage", None)
    if last_usage is not None:  # 仅有 usage 才发 USAGE；缺失也不伪造，结算责任在网关
        yield StreamEvent(kind=StreamEventKind.USAGE, data=last_usage)
    yield StreamEvent(kind=StreamEventKind.DONE)
