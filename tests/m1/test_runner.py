"""M1-T01 解释循环：resume → dispatch → call → fold → Event 顺序正确。"""
from __future__ import annotations

from runtime import (
    AgentState, Effect, EffectKind, Message, Runner, Session,
)
from tests.fakes import FakeCall, FakeDispatch, llm_response, run


def react(state: AgentState):
    while True:
        resp = yield Effect(EffectKind.LLM, {"messages": [m.to_dict() for m in state.messages]})
        if not getattr(resp, "tool_calls", None):
            yield Effect(EffectKind.END, {"output": resp.content})
            return
        yield Effect(EffectKind.TOOL, {"calls": resp.tool_calls}, batch=True)


def make():
    call = FakeCall([
        llm_response("thinking", [{"call_id": "c1", "name": "echo", "args": {}}]),
        llm_response("done"),
    ])
    return Session("t01", lambda: react(AgentState())), call


def test_t01_single_step_order():
    session, call = make()
    runner = Runner(dispatch=FakeDispatch(), call=call)
    report = run(runner.step(session))
    assert report.step == 0 and report.status == "running"
    assert [e.kind for e in report.effects] == [EffectKind.LLM]
    assert report.results[0].ok and report.events[0].state_snapshot == report.state
    assert report.state.messages[-1].role == "assistant"
    assert call.seen == [EffectKind.LLM]


def test_t01_full_react_run():
    session, call = make()
    result = run(Runner(dispatch=FakeDispatch(), call=call).run(session))
    assert result.status == "done" and result.output == "done"
    assert [e.step for e in result.events] == [0, 1, 2, 3]
    assert [e.kind.value for e in result.events[1].effects] == ["tool"]
    assert result.state.messages[-1].role == "assistant"
    assert result.state.budgets.used_turns >= 4


def test_t01_program_return_marks_done():
    def bare(state):
        if False:
            yield Effect(EffectKind.END)

    result = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).run(
        Session("bare", lambda: bare(AgentState()))
    ))
    assert result.status == "done" and result.reason == "program_returned"


def test_t01_end_effect_without_output():
    def program(state):
        yield Effect(EffectKind.END, {"reason": "stop"})

    report = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).step(
        Session("end", lambda: program(AgentState()))
    ))
    assert report.status == "done" and report.reason == "stop"


def test_t01_state_helpers_are_immutable():
    state = AgentState(messages=(Message("user", "hi"),))
    changed = state.set("phase", "plan").with_(messages=(*state.messages, Message("assistant", "ok")))
    assert state.get("phase") is None and len(state.messages) == 1
    assert changed.get("phase") == "plan" and len(changed.messages) == 2
