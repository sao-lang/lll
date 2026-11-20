"""R7 默认压缩策略：阈值触发 + 钉住保留 + 语义摘要（不沉淀）。

压缩产新消息序列（视图层），绝不修改 State.messages（内核只增不减契约）。
钉住 system、最近 N 轮与高价值消息；其余折叠为独立 role 的语义摘要，
防止摘要 role=system 被下轮按 role 永久钉住。token 计数统一走 TokenCounter。
"""
from __future__ import annotations

from typing import Protocol

from runtime import AgentState, Message

from .token_counter import DefaultTokenCounter, TokenCounter, estimate_tokens
from .types import (
    DEFAULT_KEEP_RECENT,
    DEFAULT_PIN_PATTERNS,
    DEFAULT_TRIGGER_TURNS,
    TRIGGER_RATIO,
)

__all__ = [
    "SUMMARY_ROLE",
    "CompactionPolicy",
    "DefaultCompactionPolicy",
    "count_tokens",
]

# 摘要消息角色（独立于 system，防沉淀）。
SUMMARY_ROLE = "summary"
# 摘要前缀（具名常量）。
SUMMARY_PREFIX = "[摘要] "
# 摘要保留的最近角色序列长度（用于人类可读提示）。
_SUMMARY_ROLE_WINDOW = 5


def count_tokens(text: str) -> int:
    """统一 token 估算（兼容旧调用方；与 assemble 同口径）。

    Args:
        text: 待计数文本。

    Returns:
        token 估算数。
    """
    return estimate_tokens(text)


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
    """阈值语义摘要压缩器：钉住 system/最近 N 轮/高价值，其余摘要。"""

    name: str = "default_compaction"

    def __init__(
        self,
        trigger_tokens: float = TRIGGER_RATIO,
        trigger_turns: int = DEFAULT_TRIGGER_TURNS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        pin_patterns: tuple[str, ...] = DEFAULT_PIN_PATTERNS,
        counter: TokenCounter | None = None,
    ) -> None:
        """配置压缩阈值与钉住规则。

        Args:
            trigger_tokens: token 占比阈值（0~1）。
            trigger_turns: 轮次阈值。
            keep_recent: 保留最近消息条数。
            pin_patterns: 命中即钉住的内容特征。
            counter: token 计数器；None 用 DefaultTokenCounter（与 assemble 同口径）。
        """
        self._trigger_tokens = trigger_tokens
        self._trigger_turns = trigger_turns
        self._keep_recent = keep_recent
        self._pin_patterns = pin_patterns
        self._counter = counter if counter is not None else DefaultTokenCounter()

    def should_compact(self, state: AgentState) -> bool:
        """按 token 占比或轮次判定是否压缩。

        Args:
            state: 当前会话状态。

        Returns:
            任一维度超阈值返回 True；max_tokens<=0 时该维度不触发。
        """
        max_tokens = state.budgets.max_tokens
        # 兼容两种口径：预算已记录的 used_tokens（内核权威）与消息投影估算取较大者。
        projected = self._counter.count("".join(m.content for m in state.messages))
        used = max(state.budgets.used_tokens, projected)
        token_hit = max_tokens > 0 and used / max_tokens >= self._trigger_tokens
        turn_hit = state.budgets.used_turns >= self._trigger_turns
        return token_hit or turn_hit

    def compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """产压缩视图：钉住消息原位保留，其余折叠为语义摘要。

        Args:
            messages: 原消息序列（不被修改）。

        Returns:
            新消息序列；无消息可压缩时原样返回新 tuple。
        """
        if not messages:
            return ()
        recent_start = max(0, len(messages) - self._keep_recent)
        kept_idx: set[int] = set()
        for i, msg in enumerate(messages):
            if msg.role == "system" or i >= recent_start or self._is_pinned(msg):
                kept_idx.add(i)
        dropped_idx = [i for i in range(len(messages)) if i not in kept_idx]
        if not dropped_idx:
            return tuple(messages)
        summary = self._make_summary([messages[i] for i in dropped_idx])
        insert_at = min(dropped_idx)
        out: list[Message] = []
        for i, msg in enumerate(messages):
            if i == insert_at:
                out.append(summary)
            if i in kept_idx:
                out.append(msg)
        return tuple(out)

    def _is_pinned(self, msg: Message) -> bool:
        """高价值判定：正文命中 pin_patterns 任一特征。"""
        return any(p in msg.content for p in self._pin_patterns)

    @staticmethod
    def _make_summary(dropped: list[Message]) -> Message:
        """折叠历史为语义摘要占位（独立 role，防 system 沉淀）。

        Args:
            dropped: 被丢弃的消息序列。

        Returns:
            role=summary 的摘要消息，含数量与角色分布提示。
        """
        roles = [msg.role for msg in dropped]
        detail = ",".join(roles[:_SUMMARY_ROLE_WINDOW])
        return Message(
            role=SUMMARY_ROLE,
            content=f"{SUMMARY_PREFIX}{len(dropped)} 条历史 ({detail})",
        )
