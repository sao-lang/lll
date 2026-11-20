"""R3 熔断器三态机测试：closed→open→half_open→closed + 样本量 + 失败率阈值 + 维度隔离。

领域逻辑直接测 CircuitBreaker 插件实例（状态在实例上，跨调用存活）；apply 注册三类 tap。
熔断状态存取已异步化（BreakerStore 协议），测试用同步函数 + asyncio.run 包装。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from runtime import Effect, Invoke

from harness.resilience import BreakerStatus, CircuitBreaker, InMemoryBreakerStore
from tests.r3.conftest import err_result, ok_result

DIM = ("provider", "openai")
EFFECT = Invoke(port="llm", payload={'messages': []})

def _policy(clock: object = None, **kw: object) -> CircuitBreaker:
    """构造熔断器插件实例（自带状态）；注入可控时钟推进冷却期。

    dimension_of / failed_target_of 是装配期注入的纯函数，从直送的 effect 派生
    熔断维度与失败目标注册名——不再依赖请求容器里的手填键。
    """
    opts: dict[str, object] = dict(
        minimum_samples=3, window_size=10, failure_rate=0.5,
        clock=clock or (lambda: 0.0),
        dimension_of=lambda e: DIM,
        failed_target_of=lambda e: "primary",
    )
    opts.update(kw)
    return CircuitBreaker(**opts)  # type: ignore[arg-type]

class TestCircuitBreakerStateMachine:
    """三态机：样本不足不开、阈值触发 open、冷却后半开、探测成功回 closed。"""

    def test_sample_insufficient_never_opens(self) -> None:
        """样本 < minimum_samples：即使全失败也不 open（防误判）。"""
        async def run() -> None:
            p = _policy()
            for _ in range(2):  # 2 < 3
                await p.record(err_result("500"), DIM)
            assert (await p.state(DIM)).status == BreakerStatus.CLOSED.value
        asyncio.run(run())

    def test_closed_to_open_on_threshold(self) -> None:
        """样本足够 + 失败率达阈值 → OPEN。"""
        async def run() -> None:
            p = _policy()
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            assert (await p.state(DIM)).status == BreakerStatus.OPEN.value
        asyncio.run(run())

    def test_failure_rate_below_threshold_stays_closed(self) -> None:
        """样本够但失败率低于阈值 → 保持 CLOSED。"""
        async def run() -> None:
            p = _policy(failure_rate=0.5)
            await p.record(err_result("500"), DIM)
            for _ in range(3):
                await p.record(ok_result(), DIM)
            st = await p.state(DIM)
            assert st.status == BreakerStatus.CLOSED.value
            assert st.failure_rate() == 0.25
        asyncio.run(run())

    def test_open_check_returns_degrade_reason_in_cooldown(self) -> None:
        """OPEN 且冷却未满：check 返回降级原因 breaker_open。"""
        async def run() -> None:
            now = [0.0]
            p = _policy(clock=lambda: now[0], open_cooldown_s=30.0)
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            assert await p.check(DIM) == "breaker_open"
        asyncio.run(run())

    def test_cooldown_elapsed_transitions_to_half_open(self) -> None:
        """冷却期届满：check 触发 OPEN→HALF_OPEN 且放行探测。"""
        async def run() -> None:
            now = [0.0]
            p = _policy(clock=lambda: now[0], open_cooldown_s=30.0)
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            assert await p.check(DIM) == "breaker_open"

            now[0] = 31.0  # 冷却期届满
            assert await p.check(DIM) is None
            assert (await p.state(DIM)).status == BreakerStatus.HALF_OPEN.value
        asyncio.run(run())

    def test_full_cycle(self) -> None:
        """完整周期：closed→open→(冷却)half_open→连续成功→closed。"""
        async def run() -> None:
            now = [0.0]
            p = _policy(
                clock=lambda: now[0], open_cooldown_s=30.0, half_open_success_to_close=2
            )
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            assert (await p.state(DIM)).status == BreakerStatus.OPEN.value

            now[0] = 31.0
            await p.maybe_half_open(DIM)
            assert (await p.state(DIM)).status == BreakerStatus.HALF_OPEN.value

            await p.record(ok_result(), DIM)
            await p.record(ok_result(), DIM)
            assert (await p.state(DIM)).status == BreakerStatus.CLOSED.value
        asyncio.run(run())

    def test_half_open_single_success_not_enough_to_close(self) -> None:
        """半开需连续成功达阈值才回 closed，一次成功仍保持 half_open。"""
        async def run() -> None:
            now = [0.0]
            p = _policy(
                clock=lambda: now[0], open_cooldown_s=30.0, half_open_success_to_close=2
            )
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            now[0] = 31.0
            await p.maybe_half_open(DIM)

            await p.record(ok_result(), DIM)
            st = await p.state(DIM)
            assert st.status == BreakerStatus.HALF_OPEN.value
            assert st.half_open_successes == 1
        asyncio.run(run())

    def test_half_open_probe_failure_reopens(self) -> None:
        """半开探测失败：立即重开 OPEN，冷却重新计时。"""
        async def run() -> None:
            now = [0.0]
            p = _policy(clock=lambda: now[0], open_cooldown_s=30.0)
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            now[0] = 31.0
            await p.maybe_half_open(DIM)
            await p.record(err_result("500"), DIM)

            st = await p.state(DIM)
            assert st.status == BreakerStatus.OPEN.value
            assert st.opened_at == 31.0
        asyncio.run(run())

    def test_count_false_does_not_trigger(self) -> None:
        """count=False（429 对方保护）不计入滑窗，不触发 open。"""
        async def run() -> None:
            p = _policy()
            for _ in range(5):
                await p.record(err_result("429"), DIM, count=False)
            st = await p.state(DIM)
            assert st.status == BreakerStatus.CLOSED.value
            assert st.failures == 0
            assert st.window == ()
        asyncio.run(run())

    def test_record_while_open_is_ignored(self) -> None:
        """OPEN 态的结果记录被忽略（不会污染关闭时的滑窗）。"""
        async def run() -> None:
            p = _policy()
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            before = await p.state(DIM)
            await p.record(ok_result(), DIM)
            assert await p.state(DIM) == before
        asyncio.run(run())

    def test_per_dimension_isolation(self) -> None:
        """维度隔离：一个 provider 故障不拖垮另一个。"""
        async def run() -> None:
            p = _policy()
            for _ in range(3):
                await p.record(err_result("500"), ("provider", "openai"))
            assert (await p.state(("provider", "openai"))).status == BreakerStatus.OPEN.value
            assert (await p.state(("provider", "anthropic"))).status == BreakerStatus.CLOSED.value
        asyncio.run(run())

    def test_closed_check_passes(self) -> None:
        """CLOSED 态 check 放行（返回 None）。"""
        async def run() -> None:
            p = _policy()
            assert await p.check(DIM) is None
        asyncio.run(run())

    def test_half_open_probe_limit(self) -> None:
        """半开探测超额 → check 返回 breaker_half_open。"""
        async def run() -> None:
            now = [0.0]
            p = _policy(
                clock=lambda: now[0], open_cooldown_s=30.0, half_open_max_probes=1
            )
            for _ in range(3):
                await p.record(err_result("500"), DIM)
            now[0] = 31.0
            await p.maybe_half_open(DIM)

            # HALF_OPEN 探测超额：直接经 store 置入满滑窗快照（协议 save）
            st = await p.state(DIM)
            await p._store.save(DIM, replace(
                st, status=BreakerStatus.HALF_OPEN.value,
                window=(True,), half_open_successes=0,
            ))
            assert await p.check(DIM) == "breaker_half_open"
        asyncio.run(run())

    def test_per_dimension_disabled_merges(self) -> None:
        """per_dimension=False：所有维度归并到空键。"""
        async def run() -> None:
            p = _policy(per_dimension=False)
            await p.record(err_result("500"), ("provider", "openai"))
            await p.record(err_result("500"), ("provider", "anthropic"))
            assert (await p.state(("", ""))).failures == 2
        asyncio.run(run())

    def test_window_size_is_bounded(self) -> None:
        """滑窗不超过 window_size：旧失败被滑出，失败率反映最近窗口。"""
        async def run() -> None:
            p = _policy(window_size=3, minimum_samples=5)
            for _ in range(4):
                await p.record(err_result("500"), DIM)  # 早期失败被滑出
            for _ in range(3):
                await p.record(ok_result(), DIM)
            st = await p.state(DIM)
            assert len(st.window) == 3
            assert st.status == BreakerStatus.CLOSED.value
            assert st.failures == 4
        asyncio.run(run())
