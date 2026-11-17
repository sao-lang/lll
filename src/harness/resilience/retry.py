"""R3 重试插件（AROUND）：在洋葱内部包裹 Port 调用，失败按退避算法重试。

为什么不返回 Decision.retry 让 R1 重跑：R1 内核只消费 continue/deny/abort/fallback，
retry 动作是空载体。按"内核零策略"铁律，重试的循环放在 AROUND hook 内：
反复调用 ctx.meta["next"]（最终落到 R1 注入的 ctx.invoke）。

红线：无幂等键的副作用（TOOL Effect）在 idempotency_required 时拒绝重试。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import Random

from hooks import Decision, HookCtx, HookMode, RetrySpec
from runtime import EffectKind, Result

from .types import backoff_delay, classify_failure, validate_retry_spec

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
        jitter: str = "full",
        respect_retry_after: bool = True,
        idempotency_required: bool = True,
        rng: Random | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """装配重试参数；base_delay=0 且 jitter=none 会在装配期被拒。

        参数：
            max_attempts/base_delay/multiplier/max_delay/jitter: 退避参数。
            respect_retry_after: 是否遵守服务端 Retry-After。
            idempotency_required: 副作用重试是否强制幂等键。
            rng: 注入的随机源（缺省新建，测试可注入固定种子）。
            sleeper: 异步等待函数（缺省 asyncio.sleep），避免测试真等。
        """
        self._spec = RetrySpec(
            max_attempts=max_attempts, base_delay=base_delay, multiplier=multiplier,
            max_delay=max_delay, jitter=jitter,
            respect_retry_after=respect_retry_after,
            idempotency_required=idempotency_required,
        )
        validate_retry_spec(self._spec)
        self._rng = rng if rng is not None else Random()
        self._sleep = sleeper

    def _is_retryable_side_effect(self, ctx: HookCtx) -> bool:
        """判断当前 Effect 是否为需要幂等键的副作用（TOOL）且缺少幂等键。"""
        if not self._spec.idempotency_required:
            return False
        if ctx.effect.kind is not EffectKind.TOOL:
            return False  # LLM 读操作默认可重试
        key = ctx.effect.payload.get("x-idempotency-key")
        return not bool(key)

    async def __call__(self, ctx: HookCtx) -> Decision:
        """执行重试循环：失败可重试则退避后再次调用 next()。

        参数：
            ctx: 钩子上下文；meta['next'] 是下一层回调（最终为真实 Port 调用）。
        返回：
            成功/耗尽的最终 Decision（value 装 Result）。
        """
        raw_next = ctx.meta.get("next")
        if raw_next is None:  # 无 next（独立调用）：放行，交内核正常调用
            return Decision.continue_()
        # meta 是动态边界，取出后显式标注为具名类型（编码规则 4：动态读取只留一处）。
        next_fn: Callable[[], Awaitable[Decision]] = raw_next

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
            if self._is_retryable_side_effect(ctx):
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
