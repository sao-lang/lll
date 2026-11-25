"""RED：缓存键必须含 tools_catalog，工具集变化不得命中旧响应。

独立审计 EXP7-A 证据：old_tool/new_tool 的 cache_key_string 相同，
第二次请求命中 old_tool 的响应且 backend calls=1。
"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, LLMResponse, PortCtx, ToolSpec, Usage
from harness.model.cache import ExactCache
from harness.model.gateway import ModelGateway
from harness.model.types import cache_key_string


class _ToolAwareLLM:
    name = "tool-aware"

    def __init__(self) -> None:
        self.calls = 0
        self.last_usage: Usage | None = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        names = [t.name for t in (req.tools_catalog or [])]
        self.last_usage = Usage(cost_usd=0.1)
        return LLMResponse(content="ANSWER-WITH-" + ("-".join(names) or "NO-TOOLS"), usage=self.last_usage)

    def stream(self, req: LLMRequest, ctx: PortCtx):  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            if False:
                yield ""
        return gen()


def test_red_cache_key_differs_by_tools_catalog() -> None:
    base = [{"role": "user", "content": "pick a tool"}]
    old = LLMRequest(messages=base, tools_catalog=[ToolSpec(name="old_tool")])
    new = LLMRequest(messages=base, tools_catalog=[ToolSpec(name="new_tool")])
    assert cache_key_string(old) != cache_key_string(new), (
        "tools_catalog 不同必须产出不同缓存键，实际均为 " + cache_key_string(old)
    )


def test_red_gateway_does_not_reuse_cached_response_across_tool_sets() -> None:
    """工具集变化后 backend 必须重调，返回与新工具集匹配的响应。"""
    async def run() -> None:
        llm = _ToolAwareLLM()
        gw = ModelGateway(llm=llm, cache=ExactCache())
        base = [{"role": "user", "content": "pick a tool"}]
        r1 = LLMRequest(messages=base, tools_catalog=[ToolSpec(name="old_tool")])
        r2 = LLMRequest(messages=base, tools_catalog=[ToolSpec(name="new_tool")])

        first = await gw.complete(r1, PortCtx())
        second = await gw.complete(r2, PortCtx())

        assert first.content == "ANSWER-WITH-old_tool"
        assert second.content == "ANSWER-WITH-new_tool", (
            f"工具集变了必须重调后端，实际 content={second.content!r}"
        )
        assert llm.calls == 2, f"两次不同工具集应真调后端 2 次，实际 {llm.calls}"
        assert gw.ledger.total() == 0.2

    asyncio.run(run())
