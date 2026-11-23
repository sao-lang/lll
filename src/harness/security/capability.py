"""R9 安全能力装配（rule 9：协议与默认实现成对，装配期注册 3 个 Port）。

接线：ToolsCapability.__init__ 接收 sandbox/human/audit 三 Port 注入 ToolRegistry；
本 Capability 提供开箱默认实现，install(reg) 按 PORT_PROTOCOLS 键注册。
"""
from __future__ import annotations

from compose import AuditPort, HumanPort, Registry, SandboxPort, layer_program
from runtime import Program

from .approval import ApprovalFlow
from .guard import RuleGuardrail
from .ports import AuditLogger, ConsoleHumanPort, LocalSandbox

__all__ = ["SecurityCapability"]

_PORT_SANDBOX = "sandbox"
_PORT_AUDIT = "audit"
_PORT_HUMAN = "human"


class SecurityCapability:
    """安全能力包：持三 Port 默认实现 + 护栏 + 审批流。"""
    name = "security"
    version = "9.0.0"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        sandbox: SandboxPort | None = None,
        audit: AuditPort | None = None,
        human: HumanPort | None = None,
        approval: ApprovalFlow | None = None,
        guardrail: RuleGuardrail | None = None,
    ) -> None:
        """装配能力：三 Port 缺席建默认实现（规则 9）。"""
        self.sandbox = sandbox if sandbox is not None else LocalSandbox()
        self.audit = audit if audit is not None else AuditLogger()
        self.human = human if human is not None else ConsoleHumanPort()
        self.approval = approval if approval is not None else ApprovalFlow()
        self.guardrail = guardrail if guardrail is not None else RuleGuardrail()

    def build(self) -> Program:
        """ToolLayer 外壳：安全能力的 Program 入口。"""
        return layer_program("sandbox", payload={"layer": "security"})()

    def install(self, reg: Registry) -> None:
        """把三 Port 注册进共用 Provider 注册表。"""
        reg.register(_PORT_SANDBOX, self.sandbox)
        reg.register(_PORT_AUDIT, self.audit)
        reg.register(_PORT_HUMAN, self.human)
