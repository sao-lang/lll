"""B2 行为门禁：EvalCapability 可注入且门面 score_case() 真调。

EvalPort 协议现成（compose/ports.py:237）但门面无入口；原 evaluate() 是
反思语义（ReflectionCapability.critique），不覆盖它——新增 score_case()。
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import ReAct
from compose import PortCtx, ToolSpec
from harness.eval.capability import EvalCapability
from harness.model import ModelCapability


class _SpyEval:
    name = "spy_eval"
    cases: list[tuple[str, object]] = []

    async def score(self, case_id: str, output: object,
                    ctx: PortCtx) -> float:
        self.cases.append((case_id, output))
        return 0.75


class _LLM:
    name = "fake"
    last_usage = None
    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")
    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_gate_b2_eval_capability_score_case_reaches_provider() -> None:
    spy = _SpyEval()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), session_id="gate-b2")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(EvalCapability(evaluator=spy))  # type: ignore[arg-type]

    score = asyncio.run(agent.score_case("case-1", "输出内容"))

    assert score == 0.75
    assert spy.cases == [("case-1", "输出内容")], "注入的 EvalPort 未被真调用"
