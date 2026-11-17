"""R1 框架级异常分类：一律进 StepReport，不抛穿运行时。

每个异常携带稳定 code，Runner 捕获后折叠为 Result.failure(code, ...)，
让错误以数据形态在链路里流动，而不是以栈展开打断执行。

设计约束（R1 §4）：异常只做"分类 + 稳定错误码"，不承载恢复策略；
重试/降级由上层 R3 依据 code 决定，内核不因异常中断事件溯源。
"""
from __future__ import annotations


class AgentKitError(Exception):
    """所有框架异常基类；code 是进信封的错误码。"""

    # code 写入 Result.error.code，是上层重试/降级的判定依据，一旦发布不可更改。
    code = "AGENTKIT_ERROR"


class BudgetExceeded(AgentKitError):
    """预算超限；由 Runner 预算预检触发。"""

    code = "BUDGET_EXCEEDED"  # 预算硬上限触顶，step 强制转 done 而不继续调用 Port


class ProgramInvalid(AgentKitError):
    """Program 契约违规：非 generator 或 yield 非 Effect 对象。"""

    code = "PROGRAM_INVALID"  # Program 未遵守 generator / yield Effect 的契约


class PortMissing(AgentKitError):
    """请求的 port 未注册；装配期或运行期 resolve 失败。"""

    code = "PORT_MISSING"  # 装配期或运行期找不到对应 port 适配器


class ProgramCrashed(AgentKitError):
    """Program 内部抛未预期异常；解释器包装原始信息。"""

    code = "PROGRAM_CRASHED"  # 解释器捕获到 Program 内部未预期异常


class ChainAborted(AgentKitError):
    """链路判决 abort，终止整个 run。"""

    code = "CHAIN_ABORTED"  # hook 返回 abort 判决，终止整个 run
