"""R9 安全与 HITL（security）：安全域 DTO + 护栏 + 三 Port 默认实现 + 审批流。

依赖只向下：compose + harness.skills（复用单一注入检测器）+ runtime。
不新增 Port 种类、不下沉内核、不留桩；默认实现均为真实可用实现。
"""
from __future__ import annotations

from .types import (
    AuditEvent,
    SafetyReport,
    SafetyVerdict,
    SecurityCtx,
    verify_hash_chain,
)
from .guard import Guardrail, RuleGuardrail, inspect_untrusted, redact_secret
from .ports import AuditLogger, ConsoleHumanPort, LocalSandbox
from .approval import ApprovalFlow
from .capability import SecurityCapability

__all__ = [
    "SecurityCtx", "SafetyVerdict", "SafetyReport", "AuditEvent", "verify_hash_chain",
    "Guardrail", "RuleGuardrail", "inspect_untrusted", "redact_secret",
    "LocalSandbox", "AuditLogger", "ConsoleHumanPort",
    "ApprovalFlow", "SecurityCapability",
]
