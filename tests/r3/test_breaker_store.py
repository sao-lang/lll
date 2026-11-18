"""熔断共享态存储协议测试（ARCH-v5）：异步 BreakerStore 可注入自定义实现（Redis/DB 适配器）。

状态逻辑由 CircuitBreaker 插件实例持有；store 决定状态落在进程内还是外部系统。
"""
from __future__ import annotations

import asyncio

from runtime import Result

from harness.resilience import (
    BreakerState, BreakerStatus, CircuitBreaker, InMemoryBreakerStore,
)
from tests.r3.conftest import err_result, ok_result

DIM = ("provider", "openai")


class _SpyStore:
    """自定义异步 store：记录 load/save 调用，验证熔断确实走了协议。"""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], BreakerState] = {}
        self.loads = 0
        self.saves = 0

    async def load(self, dim: tuple[str, str]) -> BreakerState | None:
        self.loads += 1
        return self.data.get(dim)

    async def save(self, dim: tuple[str, str], state: BreakerState) -> None:
        self.saves += 1
        self.data[dim] = state


class TestBreakerStoreProtocol:
    def test_default_in_memory_store(self) -> None:
        """缺省用 InMemoryBreakerStore，异步 API 正常工作。"""
        async def run() -> None:
            store = InMemoryBreakerStore()
            p = CircuitBreaker(minimum_samples=1, store=store)
            await p.record(err_result("500"), DIM)
            assert (await p.state(DIM)).status == BreakerStatus.OPEN.value

        asyncio.run(run())

    def test_custom_async_store_injected(self) -> None:
        """注入自定义异步 store：load/save 真被调用（可接 Redis/DB）。"""
        async def run() -> None:
            spy = _SpyStore()
            p = CircuitBreaker(
                minimum_samples=2, store=spy, clock=lambda: 0.0, open_cooldown_s=30.0
            )
            for _ in range(2):
                await p.record(err_result("500"), DIM)
            assert spy.saves >= 2, "自定义 store.save 未被调用"
            assert spy.loads >= 2, "自定义 store.load 未被调用"
            assert await p.check(DIM) == "breaker_open"

        asyncio.run(run())

    def test_shared_store_across_breakers(self) -> None:
        """同一 store 注入两个插件实例：熔断状态跨实例共享。"""
        async def run() -> None:
            store = InMemoryBreakerStore()
            p1 = CircuitBreaker(minimum_samples=1, store=store)
            p2 = CircuitBreaker(
                minimum_samples=1, store=store, clock=lambda: 0.0, open_cooldown_s=30.0
            )
            await p1.record(err_result("500"), DIM)
            assert (await p2.state(DIM)).status == BreakerStatus.OPEN.value
            assert await p2.check(DIM) == "breaker_open"

        asyncio.run(run())

    def test_check_and_record_are_async(self) -> None:
        """熔断公开 API 已异步化（await 不报错），check 健康返回 None。"""
        async def run() -> None:
            p = CircuitBreaker()
            await p.record(Result.success("ok"), DIM)
            assert (await p.state(DIM)).failures == 0
            assert await p.check(DIM) is None
            await p.maybe_half_open(DIM)

        asyncio.run(run())

    def test_store_reads_current_snapshot(self) -> None:
        """store 中保存的是最新不可变快照（跨次记录状态单调演进）。"""
        async def run() -> None:
            store = InMemoryBreakerStore()
            p = CircuitBreaker(minimum_samples=5, store=store)
            await p.record(err_result("500"), DIM)
            one = await store.load(DIM)
            await p.record(ok_result(), DIM)
            two = await store.load(DIM)
            assert one is not None and two is not None
            assert one.window == (True,)
            assert two.window == (True, False)
            assert one.failures == 1
            assert two.failures == 1

        asyncio.run(run())
