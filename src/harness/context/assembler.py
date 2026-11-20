"""R7 默认上下文组装器：分段排序、配额截断、贪心预算装入与 system 保护。

按 priority 升序排列各段，逐段按 quota 截断；统一 TokenCounter 计数，
超预算段降级裁剪而非整段丢弃；system 段保留先到者且设上限（不突破总预算）。
"""
from __future__ import annotations

from typing import Protocol

from harness.rag.types import TokenBudget
from runtime import Message

from .token_counter import DefaultTokenCounter, TokenCounter
from .types import ContextSection, SYSTEM_SECTION_NAME, TrimPolicy

__all__ = ["SYSTEM_SECTION_NAME", "ContextAssembler", "DefaultContextAssembler"]

# system 段配额上限（占预算比例；防止超长 system 单段突破总预算）。
_SYSTEM_BUDGET_RATIO = 0.5


class ContextAssembler(Protocol):
    """上下文组装协议：把分段投影为消息列表。"""

    name: str

    def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:
        """按配额与预算把分段组装为消息列表。"""
        ...


class DefaultContextAssembler:
    """默认组装器：优先级排序 + 配额截断 + 贪心预算 + system 保护。"""

    name: str = "default_assembler"

    def __init__(self, counter: TokenCounter | None = None) -> None:
        """构造组装器。

        Args:
            counter: token 计数器；None 用 DefaultTokenCounter（与压缩同口径）。
        """
        self._counter = counter if counter is not None else DefaultTokenCounter()

    def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:
        """把分段组装成消息列表。

        Args:
            sections: 待组装段（顺序无关，内部按 priority 排序）。
            budget: token 预算上限；limit=None 表示不限。

        Returns:
            消息列表，每段一条 Message（role 用段名，content 用裁剪后正文）。
        """
        cap = int(budget)  # None → -1（R6 TokenBudget.__int__ 语义，表示不限）
        ordered = self._dedup_system(sorted(sections, key=lambda s: s.priority))
        out: list[Message] = []
        used = 0
        for sec in ordered:
            piece = self._trim(sec)
            if sec.name == SYSTEM_SECTION_NAME:
                piece = self._cap_system(piece, cap)
                out.append(Message(role=sec.name, content=piece))
                used += self._counter.count(piece)
                continue
            # 贪心装入：预算内先整段；超预算时降级裁剪到剩余额度（非整段丢弃）。
            cost = self._counter.count(piece)
            if cap >= 0 and used + cost > cap:
                remain = max(0, cap - used)
                piece = piece[:remain]
                if not piece:
                    continue
            out.append(Message(role=sec.name, content=piece))
            used += self._counter.count(piece)
        return out

    def _cap_system(self, piece: str, cap: int) -> str:
        """system 段设上限：超长系统段也不得突破预算。

        Args:
            piece: system 段正文。
            cap: 总预算；负数表示不限。

        Returns:
            截断后的 system 正文。
        """
        if cap < 0:
            return piece
        limit = max(1, int(cap * _SYSTEM_BUDGET_RATIO))
        return piece[:limit]

    @staticmethod
    def _dedup_system(sections: list[ContextSection]) -> list[ContextSection]:
        """同 priority 下 system 段只保留先到者，防止覆盖注入。"""
        seen_system = False
        kept: list[ContextSection] = []
        for sec in sections:
            if sec.name == SYSTEM_SECTION_NAME:
                if seen_system:
                    continue
                seen_system = True
            kept.append(sec)
        return kept

    @staticmethod
    def _trim(sec: ContextSection) -> str:
        """按 trim_policy 与 quota 截断段正文。"""
        if len(sec.content) <= sec.quota:
            return sec.content
        if sec.trim_policy is TrimPolicy.HEAD:
            return sec.content[: sec.quota]
        if sec.trim_policy is TrimPolicy.PIN:
            return sec.content[: sec.quota]
        # TAIL / SUMMARY 默认保留尾部（保留最近信息，抗 context rot）。
        return sec.content[-sec.quota :]
