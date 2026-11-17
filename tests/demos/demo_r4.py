"""R4 独立 demo：模型路由 / 缓存 / 结构化修复 / 成本归因 / 换后端。

不联网（内联最小 fake LLM），全程确定性可回放。
fake 只在 tests/ 中存在，不污染框架公共 API。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import HookBus, LLMPort, LLMRequest, LLMResponse, PortCtx, Usage  # noqa: E402

from harness.model import (  # noqa: E402
    CostGuard,
    ModelCapability,
    ModelGateway,
    RuleRouter,
    StructuredOutputSpec,
    build_cache_key,
    validate_json_schema,
)


def _req() -> LLMRequest:
    """最小请求。"""
    return LLMRequest(messages=[{"role": "user", "content": "hi"}])


def _resp(content: str, cost: float = 0.01) -> LLMResponse:
    """最小响应（带成本）。"""
    return LLMResponse(content=content, usage=Usage(prompt_tokens=10, completion_tokens=5, cost_usd=cost))


def _ctx() -> PortCtx:
    """最小 PortCtx。"""
    return PortCtx(session_id="s4", run_id="r1", meta={"user_id": "u1"})


class _ScriptedLLM:
    """demo 内联最小 LLMPort fake：按队列返回预置响应（真实 fake 在 tests/r4/fakes.py）。"""

    name = "scripted_llm"

    def __init__(self, responses: list[LLMResponse]) -> None:
        """装配预置响应。"""
        self._responses = list(responses)
        self._idx = 0
        self.call_count = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """返回下一响应；用尽时重复最后一个，空队列返回空响应。"""
        self.call_count += 1
        if not self._responses:
            return LLMResponse(content="")
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        """逐字符产出最后响应正文（本 demo 不使用，仅满足协议）。"""
        content = self._responses[-1].content if self._responses else ""
        for ch in content or "":
            yield ch


SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}, "age": {"type": "integer", "minimum": 0}},
}


async def main() -> None:
    """跑通 R4 全流程并打印摘要。"""
    # 1) 缓存键含版本
    r = _req()
    assert build_cache_key(_req(), prompt_version="v1").value != build_cache_key(_req(), prompt_version="v2").value

    # 2) 缓存命中省调用
    llm = _ScriptedLLM([_resp("cached")])
    gw = ModelGateway(llm=llm)
    await gw.complete(r, _ctx())
    await gw.complete(r, _ctx())
    assert llm.call_count == 1

    # 3) 结构化修复：非法 → 合法
    llm2 = _ScriptedLLM([_resp('{"age": -1}'), _resp('{"name": "Zoe", "age": 20}')])
    gw2 = ModelGateway(llm=llm2, spec=StructuredOutputSpec(schema=SCHEMA))
    out = await gw2.complete(r, _ctx())
    assert "Zoe" in (out.content or "") and llm2.call_count == 2

    # 4) 成本归因
    assert gw2.ledger.total() > 0

    # 5) 成本闸
    guard = CostGuard(max_cost_usd=0.001)
    gw3 = ModelGateway(llm=_ScriptedLLM([_resp("x", cost=0.5)]), cost_guard=guard)
    try:
        await gw3.complete(r, _ctx())
    except ValueError:
        pass
    else:
        raise AssertionError("cost guard should block")

    # 6) 路由 + 能力装配
    router = RuleRouter(routes={"code": "gpt-4"}, registered={"gpt-4", "default"})
    cap = ModelCapability(llm=_ScriptedLLM([]), router=router)
    bus = HookBus()
    cap.install(bus)
    assert bus.registry.has("llm")

    # 7) schema 校验独立
    assert validate_json_schema('{"name": "A"}', SCHEMA) == []

    print("[R4] PASS: 缓存键/命中 / 结构化修复 / 成本归因+硬闸 / 路由 / 能力装配 / schema 校验")


if __name__ == "__main__":
    asyncio.run(main())
