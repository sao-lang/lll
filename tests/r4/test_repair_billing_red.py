"""RED：repair 循环成本必须每次后端调用只记一次账（首次 1.0 + 修复 2.0 = 3.0）。

独立审计 EXP1 证据：当前 ledger=4.0（修复响应记两次、首次响应丢失），
CostGuard.spent=4.0（应为 3.0）。测试用精确值硬断言，不接受 is not None。
"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, LLMResponse, PortCtx, Usage
from harness.model.cache import ExactCache
from harness.model.gateway import ModelGateway
from harness.model.types import CostGuard, StructuredOutputSpec

_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
}


class _SequentialLLM:
    name = "fake"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.last_usage: Usage | None = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        self.last_usage = resp.usage
        return resp

    def stream(self, req: LLMRequest, ctx: PortCtx):  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            if False:
                yield ""
        return gen()


def _resp(content: str, cost: float) -> LLMResponse:
    return LLMResponse(content=content, usage=Usage(cost_usd=cost), model="m")


def _spec() -> StructuredOutputSpec:
    return StructuredOutputSpec(schema=_SCHEMA, mode="repair", max_repair=2, strict=True)


def test_red_repair_ledger_counts_each_backend_call_exactly_once() -> None:
    """首次 + 修复共两次调用，账本总额 3.0、记录数 2。"""
    async def run() -> None:
        llm = _SequentialLLM([_resp("not-json", 1.0), _resp('{"x":1}', 2.0)])
        gw = ModelGateway(llm=llm, spec=_spec(), cache=ExactCache())
        result = await gw.complete(
            LLMRequest(messages=[{"role": "user", "content": "hi"}]), PortCtx()
        )
        assert llm.calls == 2
        assert result.error is None
        assert result.content == '{"x":1}'
        assert gw.ledger.total() == 3.0, f"首次 1.0 + 修复 2.0 应为 3.0，实际 {gw.ledger.total()}"
        assert gw.ledger.count == 2, f"两次调用应有 2 条账，实际 {gw.ledger.count}"
        assert gw.ledger.model_cost("m") == 3.0

    asyncio.run(run())


def test_red_repair_settles_cost_guard_with_full_chain_cost() -> None:
    """CostGuard 预留按整条修复链成本 3.0 结清，剩余 7.0、无悬空预留。"""
    async def run() -> None:
        llm = _SequentialLLM([_resp("not-json", 1.0), _resp('{"x":1}', 2.0)])
        guard = CostGuard(max_cost_usd=10.0)
        gw = ModelGateway(llm=llm, spec=_spec(), cache=ExactCache(), cost_guard=guard)
        await gw.complete(
            LLMRequest(messages=[{"role": "user", "content": "hi2"}]), PortCtx()
        )
        assert llm.calls == 2
        assert guard.spent == 3.0, f"guard 实扣应为 3.0，实际 {guard.spent}"
        assert guard.remaining() == 7.0
        assert guard._reserved == 0.0

    asyncio.run(run())
