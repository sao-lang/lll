"""M1-T02 确定性：同输入两次 run 事件日志哈希逐字节一致。"""
from __future__ import annotations

from agentkit.runtime import (
    AgentState, Effect, EffectKind, Runner, Session, canonical, fingerprint,
)
from tests.fakes import FakeCall, FakeDispatch, llm_response, run


def program(state):
    resp = yield Effect(EffectKind.LLM, {"messages": [], "budget": {"rounds": 1}})
    yield Effect(EffectKind.END, {"output": resp.content})


def execute():
    session = Session("det", lambda: program(AgentState()))
    call = FakeCall([llm_response("same")])
    return run(Runner(dispatch=FakeDispatch(), call=call).run(session))


def test_t02_event_log_fingerprint_identical():
    first, second = execute(), execute()
    assert fingerprint(first.events) == fingerprint(second.events)
    assert canonical(first.events) == canonical(second.events)


def test_t02_logical_clock_only():
    result = execute()
    assert [e.ts for e in result.events] == [e.step for e in result.events]
    assert all(e.seq == i for i, e in enumerate(result.events))


def test_t02_canonical_is_order_independent():
    assert canonical({"b": 1, "a": [2, {"d": 4, "c": 3}]}) == canonical(
        {"a": [2, {"c": 3, "d": 4}], "b": 1}
    )
    assert fingerprint(()) == fingerprint([])
