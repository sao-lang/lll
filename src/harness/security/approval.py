"""R9 HITL 审批流（R9 §4）：ApprovalFlow 挂起→审批→resume 状态机。

trigger_danger 决定哪一级进审批；timeout 到点按拒答（不静默放行）；
resume_strategy=exact 表示从挂起点精确恢复——由 R1 Runner 的
Suspend/resume 原语承担（本组件不重造恢复机制，只产出策略判定）。
"""
from __future__ import annotations

from compose import DangerLevel

__all__ = ["ApprovalFlow"]


class ApprovalFlow:
    """审批流策略：判定是否触发审批、超时如何裁决。

    终态恢复由 R1 Runner.resume 承担：Program yield Suspend → status=suspended →
    resume(session, answer) 精确续跑（tests/r9/test_approval.py 端到端验证）。
    """
    name = "approval_flow"

    def __init__(
        self, trigger_danger: str = "CRITICAL", timeout: int = 300,
        resume_strategy: str = "exact",
    ) -> None:
        """构造审批流。

        Args:
            trigger_danger: 触发审批的危险级别名（SAFE/LOW/HIGH/CRITICAL）。
            timeout: 审批超时秒数（0 表示立即拒答）。
            resume_strategy: exact=从挂起点精确恢复；其余值由装配期拒绝。
        """
        self.trigger_danger = DangerLevel.coerce(trigger_danger)
        self.timeout = timeout
        self.resume_strategy = resume_strategy

    def should_approve(self, danger: DangerLevel) -> bool:
        """危险级别达到触发线才审批（CRITICAL 默认；HIGH 只沙盒不审批）。"""
        return DangerLevel.coerce(danger) is self.trigger_danger

    def decide_timeout(self) -> str:
        """超时裁决：一律 deny（不静默放行）。"""
        return "deny"
