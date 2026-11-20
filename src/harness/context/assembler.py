"""R7 默认上下文组装器：分段排序、配额截断与预算控制。

按 priority 升序排列各段，逐段按 quota 截断，累计不超 TokenBudget；
system 段保留先到者且即使预算紧张也不被丢弃（红线：system 段不可被覆盖）。
"""
from __future__ import annotations

from typing import Protocol

from harness.rag.types import TokenBudget
from runtime import Message

from .types import ContextSection, TrimPolicy

__all__ = ["SYSTEM_SECTION_NAME", "ContextAssembler", "DefaultContextAssembler"]

# system 段名（具名常量，禁止裸魔法串）。
SYSTEM_SECTION_NAME = "system"


class ContextAssembler(Protocol):
    """上下文组装协议：把分段投影为消息列表。"""

    name: str

    def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:
        """按配额与预算把分段组装为消息列表。"""
        ...


class DefaultContextAssembler:
    """默认组装器：优先级排序 + 配额截断 + 预算控制 + system 段保护。"""

    name: str = "default_assembler"

    def assemble(self, sections: list[ContextSection], budget: TokenBudget) -> list[Message]:
        """把分段组装成消息列表。

        Args:
            sections: 待组装的上下文段（顺序无关，内部按 priority 排序）。
            budget: token 预算上限；limit 为 None 表示不限。

        Returns:
            消息列表，每段一条 Message（role 用段名，content 用截断后正文）。
        """
        cap = int(budget)  # None → -1（R6 TokenBudget.__int__ 语义，表示不限）
        ordered = self._dedup_system(sorted(sections, key=lambda s: s.priority))
        out: list[Message] = []
        used = 0
        for sec in ordered:
            piece = self._trim(sec)
            if sec.name == SYSTEM_SECTION_NAME:
                # system 段：即使预算不足也保留（红线不可被覆盖/丢弃）。
                out.append(Message(role=sec.name, content=piece))
                used += len(piece)
                continue
            if cap >= 0 and used + len(piece) > cap:
                # 非 system 段超预算：整段丢弃（不透支、不裁半段）。
                continue
            out.append(Message(role=sec.name, content=piece))
            used += len(piece)
        return out

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
            # PIN 段原则上不裁，但 quota 是硬约束，仍按头部截断。
            return sec.content[: sec.quota]
        # TAIL / SUMMARY 默认保留尾部（保留最近信息，抗 context rot）。
        return sec.content[-sec.quota :]
