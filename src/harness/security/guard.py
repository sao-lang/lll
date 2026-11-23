# purity-exempt-file: 护栏输入为规则3动态内容边界，DynamicValue/object 为框架既有最具体投影形态
"""R9 护栏：RuleGuardrail + 注入检测复用 + secret 脱敏（R9 §4/§6）。

关键纪律（用户拍板）：复用 harness.skills 的单一检测器 KeywordInjectionDetector，
SafetyReport 从 InjectionVerdict 派生，不造第二套。不可信输入（trust=False）
必过注入检测；可信内核（trust=True）跳过注入检测仍过敏感过滤。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from harness.skills import InjectionVerdict, KeywordInjectionDetector
from runtime import DynamicValue

from .approval import ApprovalFlow
from .types import SafetyReport, SafetyVerdict, SecurityCtx

__all__ = ["Guardrail", "RuleGuardrail", "inspect_untrusted", "redact_secret"]


@runtime_checkable
class Guardrail(Protocol):
    """护栏协议（R9 §4）：可注入，四态裁决。"""
    name: str
    direction: str

    def check(self, data: DynamicValue, ctx: SecurityCtx) -> SafetyVerdict: ...


class RuleGuardrail:
    """默认护栏：注入检测（复用单检测器）+ 敏感过滤，四态裁决。"""
    name = "rule_guardrail"
    direction = "input"

    def __init__(self, detector: KeywordInjectionDetector | None = None,
                 approval: ApprovalFlow | None = None) -> None:
        """构造护栏。

        Args:
            detector: 注入检测器；None 用 skills 包的 KeywordInjectionDetector。
            approval: 审批流；None 用默认（CRITICAL 触发审批）。
        """
        self._detector = detector or KeywordInjectionDetector()
        self._approval = approval or ApprovalFlow()

    def check(self, data: object, ctx: SecurityCtx) -> SafetyVerdict:
        """护栏：不可信内容命中注入串 → deny；含 secret 明文 → redact（fail closed）。

        secret 明文由调用方经 ctx.meta["secret_refs"] 声明（只传引用不传值是非默认
        场景；此处对确实携带的明文做最后一道脱敏，保证不进上下文/trace/持久化）。
        """
        text = data if isinstance(data, str) else str(data)
        report = inspect_untrusted(text, ctx, detector=self._detector)
        if report.level == "malicious":
            rule = report.rules_hit[0] if report.rules_hit else "injection"
            return SafetyVerdict(action="deny", reason="untrusted input rejected", rule=rule)
        # HITL：danger 达审批线 → ask（由框架产 Suspend，wait_human/resumed 通知）。
        # 审批流在这里被护栏真实消费，不再只有测试调用。
        if self._approval.should_approve(ctx.danger):
            return SafetyVerdict(action="ask", reason="human approval required", rule="approval_flow")
        declared = ctx.meta.get("secret_refs") or ()
        secrets = tuple(str(s) for s in declared if isinstance(s, str) and s)
        if secrets:
            redacted = redact_secret(text, secrets)
            if str(redacted.redacted) != text:
                return redacted
        return SafetyVerdict(action="allow")


def inspect_untrusted(
    text: str, ctx: SecurityCtx, *,
    detector: KeywordInjectionDetector | None = None,
) -> SafetyReport:
    """注入检测单一入口：trust=False 过复用检测器；trust=True 返回 safe。"""
    if ctx.trust:
        return SafetyReport(level="safe")
    detector = detector or KeywordInjectionDetector()
    verdict = detector.detect(text)
    rule = "keyword_injection" if verdict is InjectionVerdict.INJECTED else ""
    return SafetyReport.from_verdict(
        verdict, rule=rule,
        evidence=text[:80] if verdict is InjectionVerdict.INJECTED else "",
    )


def redact_secret(
    value: object, secrets: Sequence[str],
) -> SafetyVerdict:
    """敏感串脱敏：把 secrets 明文替换为 ***，返回 redact 裁决（不复用组合层 Verdict）。"""
    text = value if isinstance(value, str) else str(value)
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return SafetyVerdict(action="redact", redacted=redacted, rule="secret_filter")
