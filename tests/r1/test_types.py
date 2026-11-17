"""R1-T08：核心类型与不变量（冻结、托管字段、信封、确定性序列化）。"""
import pytest

from runtime import (
    AgentState, Budgets, Effect, EffectKind, Message, Result, Usage,
    canonical, checkpoint, fingerprint, restore_checkpoint, Session,
)


def test_state_is_immutable():
    s = AgentState()
    s2 = s.set("phase", "plan")
    assert s.get("phase") is None
    assert s2.get("phase") == "plan"


def test_managed_field():
    s = AgentState().set("output", "x")
    assert s.output == "x"
    assert s.get("output") == "x"


def test_result_envelope():
    ok = Result.success("v", usage=Usage(prompt_tokens=2, completion_tokens=3, cost_usd=0.1))
    assert ok.ok and ok.data == "v" and ok.usage is not None
    bad = Result.failure("E", "msg", retryable=True)
    assert not bad.ok and bad.error is not None and bad.error.retryable


def test_budget_consume():
    b = Budgets().consume(turns=2, cost_usd=1.5)
    assert b.used_turns == 2
    assert b.used_cost_usd == 1.5


def test_canonical_deterministic():
    assert canonical({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_checkpoint_roundtrip():
    def program(state):
        yield Effect(EffectKind.END, {"output": "done"})
    s = Session(session_id="s1", program_factory=program, state=AgentState(messages=(Message("user", "hi"),)))
    snap = checkpoint(s)
    restored = restore_checkpoint(snap, program)
    assert restored.session_id == "s1"
    assert restored.state.messages[0].content == "hi"
