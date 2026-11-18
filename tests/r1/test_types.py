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


def test_checkpoint_preserves_idempotency_key():
    """B2：checkpoint 恢复须保留 Effect.idempotency_key，否则恢复后幂等失效。"""
    def program(state):
        yield Effect(EffectKind.TOOL, {"call": {}}, idempotency_key="IDEM-KEY-123")
        yield Effect(EffectKind.END, {"output": "done"})

    eff = Effect(EffectKind.TOOL, {"call": {}}, idempotency_key="IDEM-KEY-123")
    from runtime import Event
    event = Event(
        seq=1, session_id="s-idem", step=1, state_snapshot=AgentState(),
        effects=(eff,), results=(Result.success("ok"),), status="done",
    )
    s = Session(session_id="s-idem", program_factory=program, events=(event,))
    snap = checkpoint(s)
    restored = restore_checkpoint(snap, program)
    restored_eff = restored.events[0].effects[0]
    assert restored_eff.idempotency_key == "IDEM-KEY-123", \
        f"恢复后幂等键丢失：{restored_eff.idempotency_key!r}"


def test_batch_expand_preserves_idempotency_key():
    """B3：batch 展开的子 Effect 继承幂等键，否则副作用重试红线失效。"""
    from runtime.interpreter import _expand

    parent = Effect(
        EffectKind.TOOL,
        {"calls": [{"call": 1}, {"call": 2}]},
        batch=True,
        idempotency_key="BATCH-KEY",
    )
    children = _expand(parent)
    assert len(children) == 2
    assert all(c.idempotency_key == "BATCH-KEY" for c in children), \
        f"子 Effect 幂等键丢失：{[c.idempotency_key for c in children]}"


def test_checkpoint_restore_ignores_extra_usage_keys():
    """B5：快照 usage 含额外键（如 total_tokens）时恢复不应崩溃。"""
    from runtime import Event

    eff = Effect(EffectKind.LLM, {})
    result = Result.success(
        "ok",
        usage=Usage(prompt_tokens=1, completion_tokens=2, cost_usd=0.01),
    )
    event = Event(
        seq=1, session_id="s-usage", step=1, state_snapshot=AgentState(),
        effects=(eff,), results=(result,), status="done",
    )
    s = Session(session_id="s-usage", program_factory=lambda: iter(()), events=(event,))
    snap = checkpoint(s)
    # 模拟旧快照/外部 port 写入的额外键
    snap["events"][0]["results"][0]["usage"]["total_tokens"] = 3
    restored = restore_checkpoint(snap, lambda: iter(()))
    u = restored.events[0].results[0].usage
    assert u is not None and u.prompt_tokens == 1 and u.completion_tokens == 2


def test_fold_deducts_usage_tokens_into_budget():
    """B1：usage 的 token 必须和 cost 一样扣进预算，否则 token 维形同虚设。"""
    from runtime.fold import fold

    effect = Effect(EffectKind.LLM, {"messages": []})
    result = Result.success("ok", usage=Usage(prompt_tokens=100, completion_tokens=50, cost_usd=0.5))
    state = fold(AgentState(), [effect], [result])
    assert state.budgets.used_tokens == 150, \
        f"usage token 应扣进预算，实际 used_tokens={state.budgets.used_tokens}"
    assert state.budgets.used_cost_usd == 0.5
    assert state.last_tokens == 150


def test_canonical_deterministic():
    assert canonical({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_set_is_order_independent():
    """B9：set/frozenset 需排序后序列化，跨 PYTHONHASHSEED 产出稳定。"""
    from runtime.types import _plain

    # 多个字符串在不同 PYTHONHASHSEED 下迭代顺序不同，必须排序才确定。
    words = frozenset({"ccc", "aaa", "bbb", "ddd", "eee"})
    assert _plain(words) == ["aaa", "bbb", "ccc", "ddd", "eee"]
    assert canonical({"xs": {3, 1, 2}}) == '{"xs":[1,2,3]}'


def test_checkpoint_roundtrip():
    def program(state):
        yield Effect(EffectKind.END, {"output": "done"})
    s = Session(session_id="s1", program_factory=program, state=AgentState(messages=(Message("user", "hi"),)))
    snap = checkpoint(s)
    restored = restore_checkpoint(snap, program)
    assert restored.session_id == "s1"
    assert restored.state.messages[0].content == "hi"
