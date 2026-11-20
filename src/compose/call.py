"""②③ 承载形态（规格 §2.4/§2.5）：有状态守卫 + call 装饰器 + build_call。

③ call 装饰器：CallFn → CallFn，retry（调用级）/timeout/danger_route/injection_guard；
  函数组合无固定角色链、不叫 AOP；只覆盖 Invoke（D-7）。
② 有状态守卫：Guard.check(effect,ctx)→Verdict|None + observe(effect,result,ctx)；
  由 with_guard 在调 base 前后驱动；跨调用策略状态在守卫实例，不持流程进度（D-8）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from runtime import CallFn, DynamicValue, Effect, Invoke, Result

__all__ = [
    "Guard", "Verdict", "CallFn", "with_guard", "build_call",
    "retry_call", "timeout", "danger_route", "injection_guard",
]


@dataclass(frozen=True, slots=True)
class Verdict:
    """守卫准入判决：allowed=False 拦截（R9 安全四态用 SafetyVerdict）。

    Attributes:
        allowed: 是否放行。
        reason: 拒绝原因（进 trace）。
    """
    allowed: bool = True
    reason: str = ""


@runtime_checkable
class Guard(Protocol):
    """②有状态守卫协议：check 准入、observe 后置记账。

    状态在守卫实例（跨调用存活，如熔断三态机）；禁止持有单次流程进度（D-8）。
    """
    name: str

    async def check(self, effect: Effect, ctx: object) -> Verdict | None:
        """准入判定；None 表示放行。"""
        ...

    async def observe(self, effect: Effect, result: Result, ctx: object) -> None:
        """拿到 Result 后的后置记账（熔断计数/降级选级等）。"""
        ...


class _Keys:
    """call 装饰器私有 scratch 键（x-* 约定），集中定义禁用魔法字符串。"""
    PREFIX: Final[str] = "x-compose."
    ROUTE: Final[str] = PREFIX + "danger_route"


def _ctx_meta(ctx: object) -> dict[str, DynamicValue]:
    """从 R1 RuntimeCtx 取 meta 字典（缺省空），供装饰器记录具名协作数据。"""
    meta = getattr(ctx, "meta", None)
    return meta if isinstance(meta, dict) else {}


def with_guard(base: CallFn, guard: Guard) -> CallFn:
    """用②守卫包裹 base：调前 check，拿 Result 后 observe。

    Args:
        base: 被包裹的 CallFn（③装饰器链或 dispatch）。
        guard: 有状态守卫。

    Returns:
        新 CallFn（check 拒绝时产出失败 Result，不调 base）。
    """
    # 守卫可选 reject_code：终止类守卫可声明专用拒绝码（如 INVOKE_REJECTED），
    # 缺省 GUARD_REJECTED。不污染通用 Guard 协议。
    reject_code = str(getattr(guard, "reject_code", "GUARD_REJECTED"))

    async def call(effect: Effect, ctx: object) -> Result:
        verdict = await guard.check(effect, ctx)
        if verdict is not None and not verdict.allowed:
            return Result.failure(reject_code, verdict.reason or guard.name)
        result = await base(effect, ctx)
        await guard.observe(effect, result, ctx)
        return result
    return call


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
        except asyncio.TimeoutError:
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
    guards: Sequence[Guard] = (),
) -> CallFn:
    """构造注入 R1 的 call：③装饰器链（内层先注册）+ ②守卫包裹。

    组合顺序：base → decorators[0] → ... → guards[0] → ...；
    Runner(call=build_call(...)) —— R1 一行不改。

    Args:
        base: 中心 dispatch（make_dispatch 产出）。
        decorators: ③call 装饰器序列（如 retry_call/timeout）。
        guards: ②守卫序列。

    Returns:
        符合 R1 CallFn 签名的 call。
    """
    call = base
    for deco in decorators:
        call = deco(call)
    for guard in guards:
        call = with_guard(call, guard)
    return call
