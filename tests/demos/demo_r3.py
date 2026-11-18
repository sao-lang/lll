"""R3 独立 demo：插件 apply(host) + 插件自身持有状态 + 降级信号链 + 领域防线。

不依赖外部服务：时钟/随机源/存储显式注入，全程确定性可回放。
展示 CircuitBreaker 三态机、Degradation 逐级选级（状态由插件实例自身持有），
以及失败分类、退避、幂等、watchdog、超时、重试等领域逻辑；结尾输出统一 PASS 标记。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from random import Random

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import (  # noqa: E402
    Decision, DecisionAction, HookCtx, Host, RetrySpec, Signal,
)
from runtime import AgentState, Effect, EffectKind, Result  # noqa: E402

from harness.resilience import (  # noqa: E402
    BreakerStatus,
    CircuitBreaker,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    Degradation,
    Idempotency,
    ResilienceCapability,
    RetryPolicy,
    Timeout,
    Watchdog,
    backoff_delay,
    classify_failure,
)


def _ctx() -> HookCtx:
    """构造一个最小 HookCtx（R3 回调只需 effect/state/session_id/event）。"""
    return HookCtx(
        effect=Effect(EffectKind.LLM, {"messages": []}),
        state=AgentState(), session_id="r3", event="before_invoke",
    )


def _ladder() -> DegradeLadder:
    """四级阶梯：主 → 备 → 拒答（quality 严格递减，末级必拒答）。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


class _FaultGuard:
    """故障守卫插件：emit 降级信号 + 返回占位 DEGRADE（BAIL 短路）。"""

    name = "demo_fault_guard"

    def apply(self, host: Host) -> None:
        host.hook("before_invoke").tap(self.name, self.check)

    async def check(self, scope: object, effect: Effect) -> Decision:
        scope.emit(Signal("degrade", "breaker_open", "primary"))  # type: ignore[attr-defined]
        return Decision.degrade("")


async def breaker_demo() -> None:
    """熔断三态机：closed → open → half_open → closed，状态由插件实例持有。"""
    now = [0.0]
    breaker = CircuitBreaker(
        minimum_samples=3, clock=lambda: now[0], open_cooldown_s=5.0,
        dimension_of=lambda e: ("provider", "p"),
        failed_target_of=lambda e: "primary",
    )
    host = Host()
    host.plugin(breaker).freeze()

    dim = ("provider", "p")
    eff = Effect(EffectKind.LLM, {"model": "primary", "messages": []})
    for _ in range(3):
        await breaker.record(Result.failure("500", "err"), dim)
    assert (await breaker.state(dim)).status == BreakerStatus.OPEN.value

    # OPEN 冷却内：守卫 emit 降级信号并返回占位 DEGRADE
    scope = host.new_scope()
    verdict = await host.hook("before_invoke").call(scope, eff)
    assert verdict is not None and verdict.action is DecisionAction.DEGRADE
    assert any(s.kind == "degrade" and s.reason == "breaker_open" for s in scope.signals)

    now[0] = 6.0
    await breaker.maybe_half_open(dim)
    assert (await breaker.state(dim)).status == BreakerStatus.HALF_OPEN.value
    await breaker.record(Result.success("ok"), dim)
    await breaker.record(Result.success("ok"), dim)
    assert (await breaker.state(dim)).status == BreakerStatus.CLOSED.value


async def degradation_demo() -> None:
    """降级信号链：守卫 emit → settle 读信号 → 插件选级 → scope.target。"""
    # 选级演示：每次用新实例（available 跨 Effect 累积收缩是生产行为，实例复用会互相污染）。
    assert Degradation(_ladder(), available={"primary", "backup"}).select([]) == "primary"
    primary_down = Signal("degrade", "breaker_open", "primary")
    assert Degradation(_ladder(), available={"primary", "backup"}).select([primary_down]) == "backup"
    all_down = [primary_down, Signal("degrade", "timeout", "backup")]
    assert Degradation(_ladder(), available={"primary", "backup"}).select(all_down) == DegradeMode.REFUSE.value

    # 用 host 串起完整链路：before_invoke（BAIL）→ settle（BAIL）；用全新实例。
    host = Host()
    host.plugin(_FaultGuard()).plugin(
        Degradation(_ladder(), available={"primary", "backup"})
    ).freeze()
    assert host.hook("before_invoke").names == ("demo_fault_guard",)
    assert host.hook("settle").names == ("r3_degrade",)

    effect = Effect(EffectKind.LLM, {"messages": []})
    ctx = HookCtx(
        effect=effect, state=AgentState(), session_id="r3", event="before_invoke"
    )
    verdict = await host.dispatch(effect, ctx)
    assert verdict.action is DecisionAction.DEGRADE
    assert verdict.target == "backup"
    # C2：守卫短路后 scope_bridge 已清理（本轮无 call），不残留陈旧 scope。
    assert host.scope_of(ctx) is None


async def idempotency_demo() -> None:
    """幂等：同一键在窗口内重复副作用 → DENY。"""
    host = Host()
    host.plugin(Idempotency(clock=lambda: 10.0, ttl=5.0)).freeze()
    first = host.new_scope()
    second = host.new_scope()
    first_eff = Effect(EffectKind.TOOL, {}, idempotency_key="k1")
    second_eff = Effect(EffectKind.TOOL, {}, idempotency_key="k1")
    # CONTINUE 在 BAIL 语义下不产生判决（返回 None），重复副作用才 DENY
    assert await host.hook("before_invoke").call(first, first_eff) is None
    duplicate = await host.hook("before_invoke").call(second, second_eff)
    assert duplicate is not None and duplicate.action is DecisionAction.DENY


async def watchdog_demo() -> None:
    """watchdog：重复调用超阈值 → ABORT。"""
    host = Host()
    host.plugin(Watchdog(repeat_k=3, kinds={"tool"})).freeze()
    eff = Effect(EffectKind.TOOL, {})
    verdict = None
    for _ in range(3):
        scope = host.new_scope()
        verdict = await host.hook("before_invoke").call(scope, eff)
    assert verdict is not None and verdict.action is DecisionAction.ABORT


async def timeout_demo() -> None:
    """超时：AROUND 包裹卡死的中心调用 → TIMEOUT 兜底 + degrade 信号。"""
    host = Host()
    host.plugin(Timeout(timeout=0.01)).freeze()

    async def stuck(scope: object) -> Decision:
        await asyncio.Event().wait()
        return Decision.continue_()

    scope = host.new_scope()
    eff = Effect(EffectKind.LLM, {})
    verdict = await host.hook("around_invoke").call(scope, eff, core=stuck)
    assert verdict.action is DecisionAction.FALLBACK
    assert verdict.value.error is not None and verdict.value.error.code == "TIMEOUT"
    assert any("timeout" in t for t in scope.trace)
    assert any(s.kind == "degrade" and s.reason == "timeout" for s in scope.signals)


async def retry_demo() -> None:
    """重试：AROUND 包裹失败调用，退避后重试成功。"""
    host = Host()
    host.plugin(RetryPolicy(
        max_attempts=2, base_delay=0.01, jitter="none",
        sleeper=lambda _: asyncio.sleep(0),
    )).freeze()
    calls = [0]

    async def flaky(scope: object) -> Decision:
        calls[0] += 1
        value = Result.failure("500", "err") if calls[0] == 1 else Result.success("ok")
        return Decision(value=value)

    eff = Effect(EffectKind.LLM, {})
    verdict = await host.hook("around_invoke").call(host.new_scope(), eff, core=flaky)
    assert calls[0] == 2 and verdict.value.ok is True


async def capability_demo() -> None:
    """能力装配：opt-in 传入的插件经 apply(host) 挂载，状态随插件实例自带。"""
    cap = ResilienceCapability(
        breaker=CircuitBreaker(),
        retry=RetryPolicy(),
        timeout=Timeout(timeout=0.01),
        degradation=Degradation(_ladder(), available={"primary", "backup"}),
    )
    host = cap.install(Host()).freeze()
    assert host.hook("before_invoke").names == ("r3_breaker",)
    assert host.hook("around_invoke").names == ("r3_retry", "r3_timeout")
    assert host.hook("settle").names == ("r3_degrade",)


def domain_demo() -> None:
    """失败分类 / 退避：共享设施，墙钟与随机源显式注入。"""
    fc_429 = classify_failure(Result.failure("429", "rate"), _ctx())
    fc_500 = classify_failure(Result.failure("500", "err"), _ctx())
    assert fc_429.retryable and not fc_429.count_toward_breaker
    assert fc_500.retryable and fc_500.count_toward_breaker

    spec = RetrySpec(base_delay=1.0, multiplier=2.0, max_delay=10.0, jitter="none")
    assert backoff_delay(3, spec, Random(0)) == 4.0


async def main() -> None:
    domain_demo()
    await breaker_demo()
    await degradation_demo()
    await idempotency_demo()
    await watchdog_demo()
    await timeout_demo()
    await retry_demo()
    await capability_demo()
    print("[R3] PASS: 失败分类/退避 / 熔断三态 / 降级信号链 / 幂等 / watchdog / 超时 / 重试 / 能力装配")


asyncio.run(main())
