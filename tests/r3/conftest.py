"""R3 测试夹具：共享的 Result / HookCtx / RNG / RetrySpec 构造器。

模块内闭环（CL-2）：不依赖任何外部服务，全部 fake 在此定义。
测试通过 ``from tests.r3.conftest import ...`` 引用这些纯函数。
"""
from __future__ import annotations

from random import Random

from hooks import HookCtx, RetrySpec
from runtime import AgentState, Effect, EffectKind, Result


def ok_result(data: object = None) -> Result:
    """构造成功信封（data 可为任意对象，形态由 port 决定）。"""
    return Result.success(data)


def err_result(code: str, retryable: bool = False) -> Result:
    """构造失败信封：code 为稳定错误码，retryable 为 port 显式标记。"""
    return Result.failure(code, f"err:{code}", retryable=retryable)


def make_ctx(effect: Effect | None = None, attempt: int = 0) -> HookCtx:
    """构造 HookCtx；缺省 effect 为 LLM 读操作。"""
    eff = effect or Effect(EffectKind.LLM, {"messages": []})
    return HookCtx(
        effect=eff,
        state=AgentState(),
        session_id="s-r3",
        event="before_invoke",
        attempt=attempt,
    )


def fixed_rng(seed: int = 0) -> Random:
    """固定种子随机源，保证退避测试可重复。"""
    return Random(seed)


def default_spec(**overrides: object) -> RetrySpec:
    """构造 RetrySpec 并对指定字段覆写（测试退避各分支）。"""
    base = RetrySpec()
    from dataclasses import replace

    return replace(base, **overrides)  # type: ignore[arg-type]
