"""R7 DTO 契约测试（FR-1/7/9）：字段、不可变、具名常量、secret 字段判定。"""
from __future__ import annotations

import pytest

from harness.context.types import (
    DEFAULT_KEEP_RECENT,
    DEFAULT_TRIGGER_TURNS,
    TRIGGER_RATIO,
    ContextSection,
    TrimPolicy,
)
from harness.memory.types import (
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryTier,
    SECRET_KEY_TOKENS,
    is_secret_key,
)


def test_context_section_fields_and_immutable() -> None:
    """FR-1：ContextSection 五字段齐全且 frozen。"""
    sec = ContextSection(
        name="system", content="rules", quota=100, priority=0, trim_policy=TrimPolicy.PIN
    )
    assert sec.name == "system"
    assert sec.quota == 100
    with pytest.raises(Exception):
        sec.content = "changed"  # type: ignore[misc]


def test_trim_policy_is_named_enum() -> None:
    """FR-1：trim_policy 用枚举承载，禁止裸魔法串。"""
    assert TrimPolicy.PIN.value == "pin"
    assert TrimPolicy.SUMMARY.value == "summary"
    assert TrimPolicy.HEAD.value == "head"
    assert TrimPolicy.TAIL.value == "tail"


def test_memory_enums() -> None:
    """FR-9：三层三类枚举取值固定。"""
    assert [t.value for t in MemoryTier] == ["hot", "warm", "cold"]
    assert set(k.value for k in MemoryKind) == {"episodic", "semantic", "procedural"}


def test_memory_item_frozen() -> None:
    """FR-9：MemoryItem 不可变，importance/source 可读。"""
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="用户偏好中文",
        importance=0.9,
        ts=123,
        source="session_end",
    )
    assert item.importance == 0.9
    with pytest.raises(Exception):
        item.content = "x"  # type: ignore[misc]


def test_memory_query_defaults() -> None:
    """FR-7：R7 自有召回 DTO，默认值固定。"""
    q = MemoryQuery(text="偏好")
    assert q.top_k == 5
    assert q.tier is None
    assert q.kind is None


def test_secret_key_detection() -> None:
    """FR-9：secret 键名黑名单命中（大小写不敏感），正常键放行。"""
    assert SECRET_KEY_TOKENS  # 具名常量非空
    assert is_secret_key("api_token")
    assert is_secret_key("PASSWORD")
    assert is_secret_key("db_password_hash")
    assert not is_secret_key("user_name")
    assert not is_secret_key("content")


def test_thresholds_named() -> None:
    """FR-3：压缩阈值为具名常量，非散落魔法值。"""
    assert TRIGGER_RATIO == 0.7
    assert DEFAULT_TRIGGER_TURNS == 12
    assert DEFAULT_KEEP_RECENT == 4
