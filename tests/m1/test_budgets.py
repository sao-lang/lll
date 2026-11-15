"""M1-T04 五类预算：turns/hops/rounds/cost/parallel 超限强制 END。"""
from __future__ import annotations

import pytest

from agentkit.runtime import AgentState, Budgets, Effect, EffectKind, Runner, Session
from tests.fakes import FakeCall, FakeDispatch, llm_response, run


def program(state):
    yield Effect(EffectKind.LLM, {"messages": []})
    yield Effect(EffectKind.END, {"output": "never"})


EXHAUSTED = {
    "turns": Budgets(max_turns=1, used_turns=1),
    "hops": Budgets(max_hops=2, used_hops=2),
    "rounds": Budgets(max_rounds=1, used_rounds=1),
    "cost_usd": Budgets(max_cost_usd=1.0, used_cost_usd=1.0),
    "parallel_children": Budgets(max_parallel_children=2, used_parallel_children=2),
}


@pytest.mark.parametrize("name,budgets", list(EXHAUSTED.items()))
def test_t04_budget_exceeded_forces_end(name, budgets):
    assert budgets.exceeded() == name
    session = Session(f"budget-{name}", lambda: program(AgentState()), AgentState(budgets=budgets))
    report = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).step(session))
    assert report.status == "done" and report.reason == f"BUDGET_EXCEEDED:{name}"
    assert report.effects[0].kind is EffectKind.END


def test_t04_consume_and_parallel_cap():
    b = Budgets().consume(turns=2, hops=1, rounds=1, cost_usd=0.5, parallel_children=3)
    assert (b.used_turns, b.used_hops, b.used_rounds) == (2, 1, 1)
    assert b.used_cost_usd == 0.5 and b.used_parallel_children == 3
    assert Budgets().check_parallel(9) == "parallel_children"
    assert Budgets().check_parallel(8) is None


def test_t04_declared_budget_is_enforced_by_runtime():
    def reflecting(state):
        resp = yield Effect(EffectKind.LLM, {"messages": [], "budget": {"rounds": 3}})
        yield Effect(EffectKind.END, {"output": resp.content})

    session = Session("declared", lambda: reflecting(AgentState()),
                      AgentState(budgets=Budgets(max_rounds=3)))
    result = run(Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("x")])).run(session))
    assert result.reason == "BUDGET_EXCEEDED:rounds"
    assert result.state.budgets.used_rounds == 3


def test_t04_parallel_batch_over_cap_forces_end():
    def wide(state):
        yield Effect(EffectKind.TOOL, {"calls": [{"call_id": str(i)} for i in range(9)]}, batch=True)

    session = Session("wide", lambda: wide(AgentState()))
    report = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).step(session))
    assert report.status == "done" and "parallel_children" in report.reason
