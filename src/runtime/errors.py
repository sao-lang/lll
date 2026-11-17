"""R1 框架级异常分类：一律进 StepReport，不抛穿运行时。

每个异常携带稳定 code，Runner 捕获后折叠为 Result.failure(code, ...)，
让错误以数据形态在链路里流动，而不是以栈展开打断执行。
"""
from __future__ import annotations


class AgentKitError(Exception):
    """所有框架异常基类；code 是进信封的错误码。"""
    code = "AGENTKIT_ERROR"


class BudgetExceeded(AgentKitError):
    """预算超限；由 Runner 预算预检触发。"""
    code = "BUDGET_EXCEEDED"


class ProgramInvalid(AgentKitError):
    """Program 契约违规：非 generator 或 yield 非 Effect 对象。"""
    code = "PROGRAM_INVALID"


class PortMissing(AgentKitError):
    """请求的 port 未注册；装配期或运行期 resolve 失败。"""
    code = "PORT_MISSING"


class ProgramCrashed(AgentKitError):
    """Program 内部抛未预期异常；解释器包装原始信息。"""
    code = "PROGRAM_CRASHED"


class ChainAborted(AgentKitError):
    """链路判决 abort，终止整个 run。"""
    code = "CHAIN_ABORTED"
