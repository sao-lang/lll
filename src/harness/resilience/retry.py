"""R3 重试插件（AROUND）：在洋葱内部包裹 Port 调用，失败按退避算法重试。

为什么不返回 Decision.retry 让 R1 重跑：R1 内核只消费 continue/deny/abort/fallback，
retry 动作是空载体。按"内核零策略"铁律，重试的循环放在 AROUND hook 内：
反复调用 next()（最终落到 R1 注入的 ctx.invoke）。

红线：无幂等键的副作用（TOOL Effect）在 idempotency_required 时拒绝重试。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import Random

from harness._ctxview import coerce_ctx
from hooks import Decision, HookCtx, HookMode, RetrySpec
from runtime import EffectKind, Result

from .types import Jitter, backoff_delay, classify_failure, validate_retry_spec

__all__ = ["RetryPolicy"]


class RetryPolicy:
    """重试插件：订阅 before_invoke，AROUND 模式包裹真实调用。

    属性：
        name: 插件名（强制留名）。events: 订阅事件。mode: AROUND。
        priority: 优先级（R3 §2 五道防线最内层，贴近真实调用）。
        requires_ports: 依赖端口（无）。
    """

    name = "r3_retry"
    events = ("before_invoke",)
    mode = HookMode.AROUND
    priority = 60
    requires_ports: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        multiplier: float = 2.0,
        max_delay: float = 30.0,
        jitter: Jitter | str = Jitter.FULL,
        respect_retry_after: bool = True,
        idempotency_required: bool = True,
        rng: Random | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """装配重试参数；base_delay=0 且 jitter=none 会在装配期被拒。

        参数：
            max_attempts/base_delay/multiplier/max_delay: 退避参数。
            jitter: 抖动策略，接受 Jitter 或等价字符串（跨层 RetrySpec 用 str）；
                未知值由 Jitter.coerce 显式回退到 full（规则 7）。
            respect_retry_after: 是否遵守服务端 Retry-After。
            idempotency_required: 副作用重试是否强制幂等键。
            rng: 注入的随机源（缺省新建，测试可注入固定种子）。
            sleeper: 异步等待函数（缺省 asyncio.sleep），避免测试真等。
        """
        self._spec = RetrySpec(
            max_attempts=max_attempts, base_delay=base_delay, multiplier=multiplier,
            max_delay=max_delay, jitter=Jitter.coerce(jitter).value,
            respect_retry_after=respect_retry_after,
            idempotency_required=idempotency_required,
        )
        validate_retry_spec(self._spec)
        self._rng = rng if rng is not None else Random()
        self._sleep = sleeper

    def _is_retryable_side_effect(self, ctx: HookCtx, idem_key: str | None) -> bool:
        """判断当前 Effect 是否为需要幂等键的副作用（TOOL）且缺少幂等键。

        参数：
            ctx: 钩子上下文（只读 effect.kind，具名 field）。
            idem_key: 由 coerce_ctx 具名视图提供的幂等键（不再散落 payload.get）。
        返回：
            是需要幂等键但缺失的副作用 → True。
        """
        if not self._spec.idempotency_required:
            return False
        if ctx.effect.kind is not EffectKind.TOOL:
            return False  # LLM 读操作默认可重试
        return not bool(idem_key)

    async def __call__(self, ctx: HookCtx) -> Decision:
        """执行重试循环：失败可重试则退避后再次调用 next()。

        参数：
            ctx: 钩子上下文；meta['next'] 由 coerce_ctx 读成具名 next_fn。
        返回：
            成功/耗尽的最终 Decision（value 装 Result）。
        """
        view = coerce_ctx(ctx)  # 唯一动态读取边界（规则 8）
        next_fn = view.next_fn
        if next_fn is None:  # 无 next（独立调用）：放行，交内核正常调用
            return Decision.continue_()

        sleeper = self._sleep or asyncio.sleep
        prev_delay = 0.0
        last = Decision.continue_()
        for attempt in range(1, self._spec.max_attempts + 1):
            verdict = await next_fn()
            last = verdict
            result = verdict.value
            if not isinstance(result, Result):
                return verdict  # 非 Result 载体（如 deny/abort）：交上层
            fc = classify_failure(result, ctx)
            if not fc.retryable or result.ok:
                return verdict  # 成功或确定性错误：立即返回
            ctx.trace.append(f"retry:classified:{fc.code}")
            if attempt >= self._spec.max_attempts:
                ctx.trace.append("retry:exhausted")
                return verdict  # 耗尽：返回最后一份失败 Result
            if self._is_retryable_side_effect(ctx, view.idempotency_key):
                ctx.trace.append("retry:idempotency-blocked")
                return Decision.deny("idempotency_required for side-effect retry")
            delay = backoff_delay(
                attempt, self._spec, self._rng,
                prev_delay=prev_delay, retry_after=fc.suggested_delay,
            )
            prev_delay = delay
            ctx.trace.append(f"retry:backoff:{attempt}:{delay:.4f}")
            await sleeper(delay)
        return last
