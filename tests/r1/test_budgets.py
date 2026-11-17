"""R1-T04：6 维预算超限强制 END。"""
import asyncio

import pytest

from runtime import AgentState, Budgets, Effect, EffectKind, Result, Runner, Session, Usage

DIMS = [
    ("turns", {"max_turns": 1}, {"turns": 1}),
    ("hops", {"max_hops": 1}, {"hops": 1}),
    ("rounds", {"max_rounds": 1}, {"rounds": 1}),
    ("cost_usd", {"max_cost_usd": 5.0}, {"cost_usd": 5.0}),
    ("tokens", {"max_tokens": 10}, {"tokens": 10}),
    ("parallel_children", {"max_parallel_children": 0}, {}),
]


async def fake_call(effect, ctx):
    return Result.success(None, usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.01))


async def fake_dispatch(effect, ctx):
    return None


def infinite_program(state):
    """永不主动结束的 program，逼预算超限强制 END。"""
    while True:
        yield Effect(EffectKind.LLM, {"messages": []})


@pytest.mark.parametrize("name,limits,consumption", DIMS)
def test_budget_exceeded_forces_end(name, limits, consumption):
    async def run():
        budgets = Budgets(**limits).consume(**consumption)
        init = AgentState(budgets=budgets)
        session = Session(session_id=f"b-{name}",
                          program_factory=lambda: infinite_program(init), state=init)
        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        result = await runner.run(session)
        assert result.status == "done"
        assert "BUDGET_EXCEEDED" in result.reason, f"{name}: {result.reason}"
    asyncio.run(run())
