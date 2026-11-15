"""框架级异常分类：一律进 StepReport，不抛穿运行时（M1 §5）。"""
from __future__ import annotations


class AgentKitError(Exception):
    code = "AGENTKIT_ERROR"


class BudgetExceeded(AgentKitError):
    code = "BUDGET_EXCEEDED"


class ProgramInvalid(AgentKitError):
    code = "PROGRAM_INVALID"


class PortMissing(AgentKitError):
    code = "PORT_MISSING"


class ProgramCrashed(AgentKitError):
    code = "PROGRAM_CRASHED"


class ChainAborted(AgentKitError):
    code = "CHAIN_ABORTED"
