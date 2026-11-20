"""R7 装配与顺序测试（FR-4/FR-10）：显式编排、with_ 不可变、先 cold 后 compact。"""
from __future__ import annotations

from runtime import AgentState, Budgets

from harness.context.manager import ContextMemoryManager
from harness.memory.memory import MemoryStore


def _state() -> AgentState:
    """构造够触发压缩的 state。"""
    return AgentState(budgets=Budgets(used_turns=20, max_tokens=1000))


def test_manager_defaults() -> None:
    """不注入依赖时用默认组件，可开箱调用。"""
    mgr = ContextMemoryManager()
    assert mgr.assembler.name == "default_assembler"
    assert mgr.compaction.name == "default_compaction"
    assert isinstance(mgr.memory, MemoryStore)


def test_commit_and_compact_order() -> None:
    """FR-4：trace 快照证明 write.cold 先于 compact（B1/B2 核心证据）。"""
    mgr = ContextMemoryManager()
    mgr.commit_and_compact(_state(), "完整对话记录")
    assert mgr.trace == ["write_cold", "compact"]
    assert mgr.trace.index("write_cold") < mgr.trace.index("compact")


def test_with_immutable_replacement() -> None:
    """FR-10：with_ 产新实例，替换注入的组件，原实例不变。"""
    mgr = ContextMemoryManager()
    custom = MemoryStore()
    mgr2 = mgr.with_(memory=custom)
    assert mgr2 is not mgr
    assert mgr2.memory is custom
    assert mgr.memory is not custom  # 原实例不变


def test_commit_writes_cold_full_text() -> None:
    """先写 cold：transcript 全文进入记忆（细节不丢）。"""
    mgr = ContextMemoryManager()
    mgr.commit_and_compact(_state(), "完整对话逐字记录-XYZ")
    assert mgr.memory.tier_count() >= 1


def test_assemble_via_manager() -> None:
    """assemble 走编排器默认组装器。"""
    from harness.context.types import ContextSection, TrimPolicy
    from harness.rag.types import TokenBudget

    mgr = ContextMemoryManager()
    sections = [ContextSection("system", "规则", 50, 0, TrimPolicy.PIN)]
    out = mgr.assemble(sections, TokenBudget(limit=100))
    assert out and out[0].content == "规则"
