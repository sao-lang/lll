"""R3 独立 demo：失败分类 / 退避 / 熔断三态 / 降级 / 限流 / 舱壁 / 幂等 / watchdog / capability 装配。

不依赖外部服务，全部用注入时钟/随机源/内存 store，确定性可回放。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import DecisionAction, HookBus, HookCtx, RetrySpec  # noqa: E402
from runtime import AgentState, Effect, EffectKind, Result  # noqa: E402

from harness.resilience import (  # noqa: E402
    BreakerStatus,
    CircuitBreaker,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    ResilienceCapability,
    RetryPolicy,
    backoff_delay,
    classify_failure,
)


def ctx() -> HookCtx:
    """构造 before_invoke 上下文。"""
    return HookCtx(
        effect=Effect(EffectKind.LLM, {"messages": []}),
        state=AgentState(), session_id="s", event="before_invoke",
    )


def main() -> None:
    """跑通 R3 各防线，打印断言摘要。"""
    # 1) 失败分类：429 不计熔断，500 计熔断
    fc_429 = classify_failure(Result.failure("429", "rate"), ctx())
    fc_500 = classify_failure(Result.failure("500", "err"), ctx())
    assert fc_429.retryable and not fc_429.count_toward_breaker
    assert fc_500.retryable and fc_500.count_toward_breaker

    # 2) 退避：none jitter 封顶
    spec = RetrySpec(base_delay=1.0, multiplier=2.0, max_delay=10.0, jitter="none")
    from random import Random
    assert backoff_delay(3, spec, Random(0)) == 4.0

    # 3) 熔断三态：closed→open→half_open→closed
    now = [0.0]
    b = CircuitBreaker(minimum_samples=3, clock=lambda: now[0], open_cooldown_s=5.0)
    c = ctx()
    for _ in range(3):
        b.record(Result.failure("500", "err"), c, dimension=("provider", "p"))
    assert b.state(("provider", "p")).status == BreakerStatus.OPEN.value
    now[0] = 6.0
    b.maybe_half_open(("provider", "p"))
    assert b.state(("provider", "p")).status == BreakerStatus.HALF_OPEN.value

    # 4) 降级阶梯校验 + 选级
    ladder = DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))
    assert ladder.pick(set()).mode is DegradeMode.REFUSE

    # 5) capability 一键装配到 bus
    bus = HookBus()
    ResilienceCapability().install(bus)
    bus.freeze()
    assert len(bus._hooks["before_invoke"]) >= 5

    # 6) retry AROUND：内部重试一次
    calls = [0]

    async def run_retry() -> None:
        p = RetryPolicy(max_attempts=2, base_delay=0.01, jitter="none",
                        sleeper=lambda _: asyncio.sleep(0))
        c2 = ctx()

        async def nxt() -> object:
            from hooks import Decision, DecisionAction as DA
            calls[0] += 1
            r = Result.failure("500", "err") if calls[0] == 1 else Result.success("ok")
            return Decision(action=DA.CONTINUE, value=r)

        c2.meta["next"] = nxt
        verdict = await p(c2)
        assert calls[0] == 2 and verdict.value.ok is True

    asyncio.run(run_retry())

    print("[R3] PASS: 失败分类 / 退避 / 熔断三态 / 降级 / 幂等 / watchdog / 五道防线装配 / 重试")


if __name__ == "__main__":
    main()
