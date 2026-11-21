"""R7 压缩测试（FR-3）：阈值触发、钉住保留、不改 messages。"""
from __future__ import annotations

from runtime import AgentState, Budgets, Message

from harness.context.compaction import DefaultCompactionPolicy, count_tokens
from harness.context.types import DEFAULT_KEEP_RECENT, DEFAULT_TRIGGER_TURNS, TRIGGER_RATIO

from .conftest import msg


def _state(messages: tuple[Message, ...], tokens: int = 0, turns: int = 0) -> AgentState:
    """构造带预算用量的 AgentState。"""
    budgets = Budgets(used_tokens=tokens, max_tokens=1000, used_turns=turns)
    return AgentState(messages=messages, budgets=budgets)


def test_should_compact_by_turns() -> None:
    """轮次达阈值触发压缩。"""
    policy = DefaultCompactionPolicy()
    messages = tuple(msg("user", f"第{i}轮") for i in range(DEFAULT_TRIGGER_TURNS))
    assert policy.should_compact(_state(messages, turns=DEFAULT_TRIGGER_TURNS))


def test_should_compact_by_token_ratio() -> None:
    """token 用量超窗口 70% 触发压缩。"""
    policy = DefaultCompactionPolicy()
    assert policy.should_compact(_state((), tokens=int(1000 * TRIGGER_RATIO) + 1))
    assert not policy.should_compact(_state((), tokens=int(1000 * TRIGGER_RATIO) - 100))


def test_not_compact_below_threshold() -> None:
    """未达阈值不压缩。"""
    policy = DefaultCompactionPolicy()
    messages = tuple(msg("user", f"m{i}") for i in range(3))
    assert not policy.should_compact(_state(messages, tokens=10, turns=3))


def test_compact_pins_system_and_recent() -> None:
    """压缩保留 system 段与最近 N 轮，其余被摘要替换。"""
    policy = DefaultCompactionPolicy(keep_recent=DEFAULT_KEEP_RECENT)
    messages = (
        msg("system", "核心规则"),
        msg("user", "旧问题1"),
        msg("assistant", "旧回答1"),
        msg("user", "旧问题2"),
        msg("assistant", "旧回答2"),
        msg("user", "最近1"),
        msg("assistant", "最近2"),
        msg("user", "最近3"),
        msg("assistant", "最近4"),
    )
    out = policy.compact(messages)
    contents = [m.content for m in out]
    assert "核心规则" in contents
    for recent in ["最近1", "最近2", "最近3", "最近4"]:
        assert recent in contents
    assert "旧问题1" not in contents
    assert any("摘要" in c or "..." in c for c in contents)  # 有摘要占位


def test_compact_does_not_mutate_messages() -> None:
    """红线：压缩产新 tuple，原 messages 不变（只增不减是内核契约）。"""
    policy = DefaultCompactionPolicy()
    original = (
        msg("system", "规则"),
        *(msg("user", f"m{i}") for i in range(20)),
    )
    snapshot = tuple(original)
    out = policy.compact(original)
    assert original == snapshot  # 原序列未变
    assert out is not original  # 返回新对象
    assert len(out) < len(original)


def test_count_tokens_named() -> None:
    """token 计数为具名函数，统一口径 len(text)（Unicode 码点，非字符/4）。"""
    assert count_tokens("") == 0
    assert count_tokens("abcd") >= 1
