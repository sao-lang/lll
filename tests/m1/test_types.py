"""M1 原语与纯函数补充用例（覆盖率门禁 G1）。"""
from __future__ import annotations

from enum import Enum

from agentkit.runtime import (
    DONE, AgentState, Budgets, Effect, EffectKind, Event, Message, PortError,
    Result, Runner, RuntimeCtx, RunResult, Session, StepReport, Usage, canonical,
    checkpoint, fingerprint,
)
from agentkit.runtime.fold import field, fold, replay_events, state_append_message, state_set
from agentkit.runtime.interpreter import InterpreterImpl
from agentkit.runtime.types import _plain
from tests.fakes import FakeCall, FakeDispatch, obj, run


class Color(str, Enum):
    RED = "red"


def test_effect_normalization_and_immutability():
    effect = Effect("tool", {"a": 1})
    assert effect.kind is EffectKind.TOOL
    changed = effect.with_payload(b=2)
    assert dict(changed.payload) == {"a": 1, "b": 2}
    assert dict(effect.payload) == {"a": 1}
    assert "DONE" in repr(DONE)


def test_canonical_covers_all_value_shapes():
    data = {"enum": Color.RED, "set": {1, 2}, "tuple": (1, 2), "none": None,
            "ns": obj(a=1), "msg": Message("user", "hi")}
    text = canonical(data)
    assert "red" in text and "user" in text and text == canonical(data)
    assert _plain("custom-object-with-to-dict") == "custom-object-with-to-dict"


def test_message_and_state_helpers():
    message = Message("assistant", "x", name="a", tool_calls=({"name": "t"},))
    assert message.to_dict()["tool_calls"] == [{"name": "t"}]
    state = AgentState()
    state = state_append_message(state, message)
    state = state_set(state, "cursor", 3)
    assert len(state.messages) == 1 and state.get("cursor") == 3
    assert state.get("missing", "default") == "default"
    assert Budgets().exceeded() is None


def test_fold_all_effect_kinds_and_failures():
    state = AgentState()
    effects = (
        Effect(EffectKind.SKILL, {"name": "s"}),
        Effect(EffectKind.CHILD, {"program_factory": None}),
        Effect(EffectKind.WAIT_HUMAN, {"question": "q"}),
        Effect(EffectKind.END, {"output": "final", "reason": "end"}),
    )
    results = (
        Result.success("body", Usage(2, 3, 0.5)),
        Result.failure("CHILD_ERROR", "child failed", retryable=True),
        Result.success("approved"),
        Result.success(None),
    )
    folded = fold(state, effects, results, "end")
    assert folded.get("output") == "final" and folded.get("last_reason") == "end"
    assert folded.get("last_skill") == "body" and folded.get("last_child") is None
    assert folded.budgets.used_cost_usd == 0.5 and folded.budgets.used_turns == 4
    assert folded.messages[-1].role == "system"  # 失败结果进消息，不抛穿
    assert replay_events((), state) == state
    assert field({"a": 1}, "a") == 1 and field(None, "a", "d") == "d"
    assert field(obj(a=2), "a") == 2 and field(obj(), "a", 9) == 9


def test_fold_tool_helpers_and_budget_delta():
    state = AgentState()
    state = fold(state, (Effect(EffectKind.TOOL, {"call": {"call_id": "c1"}}),),
                 (Result.success(obj(ok=True, value={"k": "v"}, error=None)),))
    assert '"value"' in state.messages[-1].content or "value" in state.messages[-1].content
    assert state.messages[-1].name == "c1"
    from agentkit.runtime.fold import fold_tool_calls, fold_tool_results
    s = fold_tool_calls(state, [{"name": "t"}])
    s = fold_tool_results(s, [obj(value=1)])
    assert s.get("pending_tool_calls") == [{"name": "t"}]


def test_dataclass_contracts():
    ctx = RuntimeCtx(Effect(EffectKind.END), AgentState(), "s1")
    assert ctx.attempt == 0 and ctx.invoke is None
    event = Event(0, "s1", 0, AgentState(), (), ())
    assert fingerprint((event,)) == fingerprint((event,))
    report = StepReport(0, AgentState())
    assert report.status == "running"
    result = RunResult("s1", AgentState(), "done", ())
    assert result.output is None
    session = Session("s1", lambda: iter(()))
    assert checkpoint(session)["fingerprint"]
    assert PortError("X", "y").retryable is False


def test_interpreter_started_and_child_runner_paths():
    def program(state):
        yield Effect(EffectKind.END, {"output": "x"})

    interp = InterpreterImpl(program(AgentState()))
    assert interp.started is False
    assert interp.send(None).effects[0].kind is EffectKind.END
    assert interp.started is True
    assert interp.send(None) is DONE

    def child(state):
        yield Effect(EffectKind.END, {"output": "child"})

    def parent(state):
        answer = yield Effect(EffectKind.CHILD, {"program_factory": lambda: child(AgentState())})
        yield Effect(EffectKind.END, {"output": answer})

    result = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).run(
        Session("parent", lambda: parent(AgentState()))
    ))
    assert result.output == "child"
    assert any(":child:" in e.session_id for e in result.events)
