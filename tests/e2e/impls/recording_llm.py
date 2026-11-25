"""用户侧自定义 LLMPort：真履约 complete/stream，记录每次实收的 messages。

非测试桩：complete 从 messages 计算确定性答复，stream 真逐块产出，last_usage 真结算。
可观测副作用：每轮 messages 原文、调用计数、usage，供门面 recall/write_after 用例断言。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from compose import LLMRequest, LLMResponse, PortCtx, Usage


class RecordingLLM:
    """履约 LLMPort + LLMPortWithUsage：记录实收 messages 的自定义 LLM。"""

    name = "recording_llm"

    def __init__(self, *, answer: str = "OK", seed_prefix: str = "") -> None:
        self.received_messages: list[list[dict]] = []
        self.complete_calls = 0
        self.stream_calls = 0
        self.last_usage: Usage | None = None
        self._answer = answer
        self._seed_prefix = seed_prefix

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.complete_calls += 1
        # 深拷贝快照：门面后续会改 messages，必须留调用当时的原文
        self.received_messages.append([dict(m) for m in req.messages])
        content = self._answer
        usage = Usage(prompt_tokens=sum(len(str(m.get("content", ""))) for m in req.messages) or 1,
                      completion_tokens=len(content) or 1)
        self.last_usage = usage
        return LLMResponse(content=content, usage=usage, model="recording")

    async def _stream_impl(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        self.stream_calls += 1
        self.received_messages.append([dict(m) for m in req.messages])
        text = self._answer or "OK"
        for ch in text:
            yield ch
        self.last_usage = Usage(prompt_tokens=len(text) or 1, completion_tokens=len(text) or 1)

    def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        return self._stream_impl(req, ctx)

    def system_text(self, call_index: int = -1) -> str:
        """取某次调用实收的 system 消息文本（精确断言 recall 注入）。"""
        msgs = self.received_messages[call_index]
        return " ".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system")
