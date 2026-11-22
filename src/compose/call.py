"""③ call 装饰器（规格 §2.4）：CallFn → CallFn 纯函数组合 + build_call。

retry（调用级）/timeout/danger_route/injection_guard 均为函数组合，无固定角色链；
只覆盖 Invoke（D-7）。R3 可靠性由 harness.resilience.ReliabilityExecutor 显式编排。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from runtime import CallFn, DynamicValue, Effect, Invoke, Result

__all__ = [
    "CallFn", "build_call",
    "retry_call", "timeout", "danger_route", "injection_guard",
]


class _Keys:
    """call 装饰器私有 scratch 键（x-* 约定），集中定义禁用魔法字符串。"""
    PREFIX: Final[str] = "x-compose."
    ROUTE: Final[str] = PREFIX + "danger_route"


def _ctx_meta(ctx: object) -> dict[str, DynamicValue]:
    """从 R1 RuntimeCtx 取 meta 字典（缺省空），供装饰器记录具名协作数据。"""
    meta = getattr(ctx, "meta", None)
    return meta if isinstance(meta, dict) else {}


def retry_call(
    base: CallFn,
    *,
    attempts: int = 3,
    backoff: float = 0.0,
    retry_on: Callable[[Result], bool] | None = None,
) -> CallFn:
    """③调用级 retry：失败可重试时循环调 base（与 A 类流程级 retry 区分，D-5）。

    Args:
        base: 被包裹 CallFn。
        attempts: 最大尝试次数（≥1）。
        backoff: 每次退避秒数（>0 时 asyncio.sleep）。
        retry_on: 判定是否重试的纯函数，缺省按 Result.error.retryable。

    Returns:
        新 CallFn。
    """
    should_retry = retry_on or (lambda r: bool(r.error and r.error.retryable))

    async def call(effect: Effect, ctx: object) -> Result:
        result: Result | None = None
        for _ in range(max(1, attempts)):
            result = await base(effect, ctx)
            if result.ok or not should_retry(result):
                return result
            if backoff > 0:
                await asyncio.sleep(backoff)
        return result or Result.failure("RETRY_EXHAUSTED", "no attempt")

    return call


def timeout(base: CallFn, *, seconds: float) -> CallFn:
    """③调用级 timeout：wait_for 包 base（R1 无 wait_for 限制见 D-5）。"""
    async def call(effect: Effect, ctx: object) -> Result:
        try:
            return await asyncio.wait_for(base(effect, ctx), timeout=seconds)
        except TimeoutError:
            return Result.failure("TIMEOUT", f"call exceeded {seconds}s")
    return call


def danger_route(base: CallFn, registry: object) -> CallFn:
    """③危险路由：按 payload.danger 经 registry 路由到沙箱 Port（沙箱是⑤Port）。"""
    async def call(effect: Effect, ctx: object) -> Result:
        resolve = getattr(registry, "resolve", None)
        if callable(resolve) and isinstance(effect, Invoke):
            _ctx_meta(ctx)[_Keys.ROUTE] = effect.payload.get("danger", "")
        return await base(effect, ctx)
    return call


def injection_guard(base: CallFn, detector: object) -> CallFn:
    """③注入检测：不可信输入过 detector，命中则拦截（fail-closed）。"""
    async def call(effect: Effect, ctx: object) -> Result:
        detect = getattr(detector, "detect", None)
        if callable(detect) and isinstance(effect, Invoke):
            verdict = detect(effect.payload, ctx)
            allowed = getattr(verdict, "allowed", None)
            if allowed is False:
                return Result.failure("INJECTION_DETECTED", "untrusted input rejected")
        return await base(effect, ctx)
    return call


def build_call(
    base: CallFn,
    *,
    decorators: Sequence[Callable[[CallFn], CallFn]] = (),
) -> CallFn:
    """构造注入 R1 的 call：③装饰器链（内层先注册）。

    组合顺序：base → decorators[0] → ... → decorators[n]；
    Runner(call=build_call(...)) —— R1 一行不改。

    Args:
        base: 中心 dispatch（make_dispatch 产出）。
        decorators: call 装饰器序列（如 retry_call/timeout）。

    Returns:
        符合 R1 CallFn 签名的 call。
    """
    call = base
    for deco in decorators:
        call = deco(call)
    return call
