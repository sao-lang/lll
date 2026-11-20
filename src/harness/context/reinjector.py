"""R7 重注入：把语义摘要、钉住片段与未完成任务回注下一轮上下文。

重注入是 R7 编排内部步骤（无通知点），必须按 budget 二次裁剪，
不得绕过预算导致组装后超窗口；产物作为下一轮 assemble 的输入段。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.rag.types import TokenBudget
from runtime import Message

from .token_counter import DefaultTokenCounter, TokenCounter

__all__ = ["DefaultReinjector", "Reinjector"]

# 摘要消息的角色标记（独立于 system，防止下轮 compact 按 role 永久钉住）。
SUMMARY_ROLE = "summary"
# 钉住消息的角色标记。
PINNED_ROLE = "pinned"
# 回注段配额占预算的比例上限（防止重注入挤占主任务）。
_REINJECT_BUDGET_RATIO = 0.3


@runtime_checkable
class Reinjector(Protocol):
    """重注入协议（context 包内部协议，不进 PORT_PROTOCOLS）。"""

    name: str

    def reinject(
        self,
        view: tuple[Message, ...],
        summary: Message | None,
        tasks: tuple[str, ...],
        budget: TokenBudget,
    ) -> tuple[Message, ...]:
        """按预算把摘要+钉住片段+未完成任务回注视图。"""
        ...


class DefaultReinjector:
    """默认重注入器：按 token 预算贪心装入，超出丢弃低优先级回注项。"""

    name: str = "default_reinjector"

    def __init__(self, counter: TokenCounter | None = None) -> None:
        """构造重注入器。

        Args:
            counter: token 计数器；None 用 DefaultTokenCounter。
        """
        self._counter = counter if counter is not None else DefaultTokenCounter()

    def reinject(
        self,
        view: tuple[Message, ...],
        summary: Message | None,
        tasks: tuple[str, ...],
        budget: TokenBudget,
    ) -> tuple[Message, ...]:
        """回注摘要、钉住片段与未完成任务，按预算二次裁剪。

        Args:
            view: 当前压缩视图（不被修改）。
            summary: 语义摘要消息，None 表示无摘要。
            tasks: 未完成任务文本序列。
            budget: token 预算；limit 为 None 表示不限。

        Returns:
            新消息 tuple：原视图 + 预算内可装入的回注消息。
        """
        cap = int(budget)
        extra_budget = cap if cap < 0 else int(cap * _REINJECT_BUDGET_RATIO)
        out = list(view)
        used = 0
        candidates: list[Message] = []
        if summary is not None:
            candidates.append(Message(role=SUMMARY_ROLE, content=summary.content))
        candidates.extend(Message(role=PINNED_ROLE, content=text) for text in tasks)
        for msg in candidates:
            cost = self._counter.count(msg.content)
            if cap >= 0 and used + cost > extra_budget:
                continue  # 超预算跳过低优先级回注项，不透支
            out.append(msg)
            used += cost
        return tuple(out)
