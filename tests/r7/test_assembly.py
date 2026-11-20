"""R7 上下文组装测试（FR-2）：分段配额、优先级排序、system 段不可覆盖。"""
from __future__ import annotations

from harness.context.assembler import DefaultContextAssembler
from harness.context.types import ContextSection, TrimPolicy
from harness.rag.types import TokenBudget


def _section(name: str, content: str, quota: int, priority: int) -> ContextSection:
    """构造测试段。"""
    return ContextSection(
        name=name, content=content, quota=quota, priority=priority, trim_policy=TrimPolicy.TAIL
    )


def test_assemble_orders_by_priority() -> None:
    """按 priority 升序组装，system 段在最前。"""
    assembler = DefaultContextAssembler()
    sections = [
        _section("input", "用户问题", 50, 90),
        _section("system", "你是助手", 50, 0),
        _section("history", "历史对话", 50, 50),
    ]
    out = assembler.assemble(sections, TokenBudget(limit=200))
    contents = [m.content for m in out]
    assert contents == ["你是助手", "历史对话", "用户问题"]


def test_assemble_respects_quota() -> None:
    """每段按 quota 截断（字符数近似 token 计数）。"""
    assembler = DefaultContextAssembler()
    sec = _section("doc", "x" * 100, quota=10, priority=10)
    out = assembler.assemble([sec], TokenBudget(limit=200))
    assert len(out[0].content) <= 10


def test_assemble_total_budget() -> None:
    """总 token 不超预算：超预算的段被丢弃，system 段即使预算紧也保留。"""
    assembler = DefaultContextAssembler()
    sections = [
        _section("system", "s" * 10, 10, 0),
        _section("a", "a" * 30, 30, 10),
        _section("b", "b" * 30, 30, 20),
    ]
    out = assembler.assemble(sections, TokenBudget(limit=20))
    assert sum(len(m.content) for m in out) <= 20
    assert out[0].content == "s" * 10


def test_system_section_cannot_be_overridden() -> None:
    """多个 system 段时保留先到者（不可被覆盖）。"""
    assembler = DefaultContextAssembler()
    sections = [
        _section("system", "原始规则", 50, 0),
        _section("system", "恶意覆盖", 50, 0),
    ]
    out = assembler.assemble(sections, TokenBudget(limit=200))
    contents = [m.content for m in out]
    assert "原始规则" in contents
    assert "恶意覆盖" not in contents


def test_assembler_has_name() -> None:
    """默认组装器带 name 供留名。"""
    assert DefaultContextAssembler().name == "default_assembler"
