"""A2 行为门禁：输出脱敏复用注入的 guardrail（不是新建 RuleGuardrail）。

AgentBuilder 注入 SecurityCapability(guardrail=spy) → redact() → spy.check 收到。
"""
from __future__ import annotations

from api import AgentBuilder
from compose import PortCtx
from harness.security import SecurityCapability, SafetyVerdict


class _SpyGuard:
    """履约 Guardrail：check(data, ctx) → SafetyVerdict。"""
    name = "spy_guard"
    direction = "output"
    checks: list[object] = []

    def check(self, data: object, ctx: object) -> SafetyVerdict:
        self.checks.append(data)
        return SafetyVerdict(action="allow", rule="spy")


class _StubArch:
    def compile(self):  # type: ignore[no-untyped-def]
        from runtime import Terminate
        def p():  # type: ignore[no-untyped-def]
            yield Terminate(output="ok")
        return p()


def test_gate_a2_output_redaction_uses_injected_guardrail() -> None:
    guard = _SpyGuard()
    builder = AgentBuilder(_StubArch())
    builder.capability(SecurityCapability(guardrail=guard))  # type: ignore[arg-type]
    builder.secrets(["sk-secret-123"])

    verdict = builder.redact("密钥 sk-secret-123", PortCtx(session_id="a2"))

    assert guard.checks, "输出脱敏未调用注入的 guardrail（用了新建默认实例）"
    assert verdict.rule == "spy", "裁决来自新建默认护栏而非注入实例"
