"""R4 测试专用 fake LLM：录制响应 / 预置流式块。

只在 tests/ 与 demos/ 内使用，不属于框架公共 API。
真实后端（OpenAI-compat 等）由注入方实现 LLMPort 协议提供，框架不内置 fake。
不联网、确定性可回放，保证 R4 测试模块内闭环（CL-2）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from hooks import LLMRequest, LLMResponse, PortCtx

__all__ = ["RecordingLLM", "StreamLLM"]


class RecordingLLM:
    """录制式 LLM：按队列返回预置响应，记录调用次数（complete 契约）。

    属性：
        name: 适配器名。call_count: 已调用次数。
    """

    name = "recording_llm"

    def __init__(self, responses: list[LLMResponse]) -> None:
        """装配预置响应队列。"""
        self._responses = list(responses)
        self._idx = 0
        self.call_count = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """返回下一个预置响应；队列用尽时重复最后一个。"""
        self.call_count += 1
        if self._responses:
            resp = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            return resp
        return LLMResponse(content="")

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """流式：把最后响应的 content 按字符块产出（缺省空）。"""
        content = self._responses[-1].content if self._responses else ""
        for ch in content or "":
            yield ch


class StreamLLM:
    """流式 LLM：按预置块逐块产出，验证取消可中断。"""

    name = "stream_llm"

    def __init__(self, chunks: list[str]) -> None:
        """装配流式块列表。"""
        self._chunks = list(chunks)

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次性补全：拼接全部块。"""
        return LLMResponse(content="".join(self._chunks))

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """逐块产出；aclose() 可随时中断（由 async generator 语义保证）。"""
        for chunk in self._chunks:
            yield chunk
