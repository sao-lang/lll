"""R7 默认压缩策略：阈值触发 + 钉住保留 + 摘要其余。

压缩产新消息序列（视图层），绝不修改 State.messages（内核只增不减契约）。
钉住 system 段、最近 N 轮与高价值（pin_patterns 命中）消息，其余折叠为摘要占位。
"""
from __future__ import annotations

from typing import Protocol

from runtime import AgentState, Message

from .types import DEFAULT_KEEP_RECENT, DEFAULT_PIN_PATTERNS, DEFAULT_TRIGGER_TURNS, TRIGGER_RATIO

__all__ = [
    "SUMMARY_PREFIX",
    "CompactionPolicy",
    "DefaultCompactionPolicy",
    "count_tokens",
]

# 摘要占位前缀（具名常量）。
SUMMARY_PREFIX = "[摘要] "


def count_tokens(text: str) -> int:
    """粗略估算 token 数（每 4 字符约 1 token，向上取整）。"""
    return (len(text) + 3) // 4


class CompactionPolicy(Protocol):
    """压缩策略协议：判定是否压缩 + 产压缩视图。"""

    name: str

    def should_compact(self, state: AgentState) -> bool:
        """判定当前状态是否超压缩阈值。"""
        ...

    def compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """产压缩后的消息视图（新 tuple，不改入参）。"""
        ...


class DefaultCompactionPolicy:
    """阈值摘要压缩器：钉住 system/最近 N 轮/高价值，其余摘要。"""

    name: str = "default_compaction"

    def __init__(
        self,
        trigger_tokens: float = TRIGGER_RATIO,
        trigger_turns: int = DEFAULT_TRIGGER_TURNS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        pin_patterns: tuple[str, ...] = DEFAULT_PIN_PATTERNS,
    ) -> None:
        """配置压缩阈值与钉住规则。

        Args:
            trigger_tokens: token 占比阈值（0~1）。
            trigger_turns: 轮次阈值。
            keep_recent: 保留最近消息条数。
            pin_patterns: 命中即钉住的内容特征。
        """
        self._trigger_tokens = trigger_tokens
        self._trigger_turns = trigger_turns
        self._keep_recent = keep_recent
        self._pin_patterns = pin_patterns

    def should_compact(self, state: AgentState) -> bool:
        """按 token 占比或轮次判定是否压缩。

        Args:
            state: 当前会话状态。

        Returns:
            任一维度超阈值返回 True。max_tokens<=0 时该维度不触发（防除零）。
        """
        max_tokens = state.budgets.max_tokens
        token_hit = (
            max_tokens > 0 and state.budgets.used_tokens / max_tokens >= self._trigger_tokens
        )
        turn_hit = state.budgets.used_turns >= self._trigger_turns
        return token_hit or turn_hit

    def compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """产压缩视图：钉住消息原位保留，其余折叠为一条摘要。

        Args:
            messages: 原消息序列（不被修改）。

        Returns:
            新消息序列；无消息可压缩时原样返回新 tuple。
        """
        if not messages:
            return ()
        recent_start = max(0, len(messages) - self._keep_recent)
        kept_idx: set[int] = set()
        for i, m in enumerate(messages):
            if m.role == "system" or i >= recent_start or self._is_pinned(m):
                kept_idx.add(i)
        dropped_idx = [i for i in range(len(messages)) if i not in kept_idx]
        if not dropped_idx:
            return tuple(messages)
        summary = self._make_summary([messages[i] for i in dropped_idx])
        insert_at = min(dropped_idx)
        out: list[Message] = []
        for i, m in enumerate(messages):
            if i == insert_at:
                out.append(summary)
            if i in kept_idx:
                out.append(m)
        return tuple(out)

    def _is_pinned(self, m: Message) -> bool:
        """高价值消息判定：正文命中 pin_patterns 任一特征。"""
        return any(p in m.content for p in self._pin_patterns)

    @staticmethod
    def _make_summary(dropped: list[Message]) -> Message:
        """把被丢弃消息折叠为一条摘要占位（保留数量与角色分布提示）。"""
        roles = [m.role for m in dropped]
        detail = ",".join(roles[:5])
        return Message(role="system", content=f"{SUMMARY_PREFIX}{len(dropped)} 条历史 ({detail})")
