"""M1-T03 回放恢复：事件重放后 State 与终态全等 + checkpoint 跨周期恢复。"""
from __future__ import annotations

from runtime import (
    AgentState, Budgets, Effect, EffectKind, Message, Replayed, Runner, Session,
    Usage, canonical, checkpoint, restore_checkpoint, restore_state, revive,
)
from runtime.types import _plain
from tests.fakes import FakeCall, FakeDispatch, llm_response, run


def program(state):
    resp = yield Effect(EffectKind.LLM, {"messages": []})
    yield Effect(EffectKind.END, {"output": resp.content})


def test_t03_replay_equals_final_state():
    session = Session("replay", lambda: program(AgentState()))
    runner = Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("ok")]))
    result = run(runner.run(session))
    assert runner.replay(session, AgentState()) == result.state


def test_t03_checkpoint_roundtrip():
    session = Session("ckpt", lambda: program(AgentState()))
    runner = Runner(dispatch=FakeDispatch(), call=FakeCall([llm_response("ok")]))
    result = run(runner.run(session))
    snap = checkpoint(session)
    assert snap["fingerprint"] and canonical(snap["events"]) == canonical(result.events)
    restored = restore_checkpoint(snap, lambda: program(AgentState()))
    assert restored.state == result.state
    assert canonical(restored.events) == canonical(result.events)
    assert runner.load("ckpt", lambda: program(AgentState())).state == result.state


def test_t03_revive_supports_attribute_access():
    value = revive({"content": "x", "tool_calls": [{"name": "echo", "args": {"a": 1}}]})
    assert isinstance(value, Replayed)
    assert value.content == "x" and value.tool_calls[0]["name"] == "echo"
    assert revive([1, {"a": 2}])[1].a == 2
    try:
        _ = value.missing
        raise AssertionError("应抛 AttributeError")
    except AttributeError:
        pass


def test_t03_restore_state_keeps_tuple_and_mapping():
    state = AgentState(
        messages=(Message("assistant", "x", tool_calls=({"name": "t"},)),),
        scratchpad={"k": "v"},
        budgets=Budgets(used_cost_usd=1.25),
        version="1.2.3",
    )
    restored = restore_state(_plain(state))
    assert restored == state
    assert isinstance(restored.messages[0].tool_calls, tuple)
    assert restored.budgets.used_cost_usd == 1.25 and Usage(1, 2, 0.5).cost_usd == 0.5
