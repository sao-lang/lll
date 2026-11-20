"""R3 AOP 角色方法覆盖：CircuitBreaker guard/observe/wrap、
extra 插件各角色分派。这些方法在 P4 插件迁入 AOP 后承担核心语义。
"""
from __future__ import annotations

import asyncio

from compose.context import Invocation, Verdict

from compose import Envelope, Prelude, Refiner, Warden
from runtime import AgentState, Budgets, Invoke, Result

from harness.resilience import (
    BreakerStatus, CircuitBreaker, InMemoryBreakerStore,
    ResilienceCapability,
)
from tests.r3.conftest import err_result, make_ctx, ok_result

DIM = ("provider", "openai")


class TestBreakerRoles:
    def test_guard_healthy_no_fault(self) -> None:
        """Warden.guard 健康：放行、不写 fault。"""
        cb = CircuitBreaker(minimum_samples=3)
        inv = make_ctx()
        verdict = asyncio.run(cb.guard(inv))
        assert verdict.allowed is True and inv.fault() is None

    def test_guard_open_writes_fault_with_registered_target(self) -> None:
        """Warden.guard OPEN：写 fault（reason + failed_target 注册名，B2）并放行重路由。"""
        now = [0.0]
        cb = CircuitBreaker(
            minimum_samples=3, clock=lambda: now[0], open_cooldown_s=30.0,
            dimension_of=lambda inv: DIM,
        )
        for _ in range(3):
            asyncio.run(cb.record(err_result("500"), DIM))
        inv = make_ctx(Invoke(port="llm", payload={"model": "primary"}))
        verdict = asyncio.run(cb.guard(inv))
        assert verdict.allowed is True
        fault = inv.fault()
        assert fault is not None and fault.reason == "breaker_open"
        assert fault.failed_target == "primary"

    def test_observe_records_result(self) -> None:
        """Envelope 后置 observe：从 Result 记数（失败计入滑窗）。"""
        cb = CircuitBreaker(minimum_samples=3)
        inv = make_ctx()
        asyncio.run(cb.observe(inv, err_result("500")))
        st = asyncio.run(cb.state(()))  # dimension_of 未注入 → 空维度
        assert st.failures == 1 and st.window == (True,)

    def test_wrap_records_after_center(self) -> None:
        """Envelope.wrap：中心调用后记数（成功 Result）。"""
        cb = CircuitBreaker(minimum_samples=3)
        inv = make_ctx()
        async def core(i: Invocation) -> Result:
            return ok_result()
        result = asyncio.run(cb.wrap(inv, core))
        assert result.ok
        assert asyncio.run(cb.state(())).failures == 0

    def test_wrap_records_failure_on_exception(self) -> None:
        """Envelope.wrap：中心抛异常按失败记数并原样抛回。"""
        cb = CircuitBreaker(minimum_samples=3)
        inv = make_ctx()
        async def boom(i: Invocation) -> Result:
            raise RuntimeError("boom")
        try:
            asyncio.run(cb.wrap(inv, boom))
        except RuntimeError:
            pass
        else:
            raise AssertionError("wrap must re-raise core exception")
        assert asyncio.run(cb.state(())).failures == 1

    def test_dimension_of_non_tuple_falls_back(self) -> None:
        """dimension_of 返回非二元组时回退空维度（不污染 store key）。"""
        cb = CircuitBreaker(dimension_of=lambda inv: "not-a-tuple")  # type: ignore[arg-type]
        assert cb._dimension(make_ctx()) is None


class TestExtraPluginRoleDispatch:
    def test_extra_roles_dispatched(self) -> None:
        """extra_plugins 中的五角色实例按角色分派（覆盖鸭子分派分支）。"""
        from compose import AOPContainer
        class CW(Warden):
            name = "cw"
            async def guard(self, inv): return Verdict(allowed=True)
        class CE(Envelope):
            name = "ce"
            async def wrap(self, inv, next): return await next(inv)
        class CR(Refiner):
            name = "cr"
            async def refine(self, inv): return inv
        class CP(Prelude):
            name = "cp"
            async def run(self, inv, next): return await next(inv)
        class CS:
            name = "cs"
            async def recover(self, exc, inv): return Result.success("x")
        cap = ResilienceCapability(extra_plugins=(CW(), CE(), CR(), CP(), CS()))
        c = AOPContainer(lambda inv: Result.success("x"))
        cap.install(c)
        assert "cw" in [getattr(x, "name", "") for x in c._wardens]
        assert "ce" in [getattr(x, "name", "") for x in c._envelopes]
        assert "cr" in [getattr(x, "name", "") for x in c._refiners]
        assert "cp" in [getattr(x, "name", "") for x in c._preludes]
        assert "cs" in [getattr(x, "name", "") for x in c._salvages]

    def test_multi_envelope_composition_no_breakpoint(self) -> None:
        """多 Envelope（Bulkhead/Retry/Timeout）+ Salvage 组合执行无断点。"""
        from compose import AOPContainer
        from harness.resilience import (
            Bulkhead, DegradeLadder, DegradeMode, DegradeStep, Degradation, RetryPolicy, Timeout,
        )
        ladder = DegradeLadder(steps=(
            DegradeStep("primary", DegradeMode.REROUTE, 1.0),
            DegradeStep("backup", DegradeMode.REROUTE, 0.6),
            DegradeStep("", DegradeMode.REFUSE, 0.0),
        ))
        cap = ResilienceCapability(
            bulkhead=Bulkhead(pools={"default": 5}),
            retry=RetryPolicy(max_attempts=2, base_delay=0.001, jitter="none"),
            timeout=Timeout(timeout=5.0),
            degradation=Degradation(ladder, available={"primary", "backup"}),
        )
        calls: list[int] = []
        async def core(inv: Invocation) -> Result:
            calls.append(1)
            return Result.success("ok") if len(calls) >= 2 else Result.failure("500", "transient")

        container = AOPContainer(core)
        cap.install(container)
        inv = make_ctx()
        result = asyncio.run(container.run_invocation(inv))
        assert result.ok and result.data == "ok"
        assert len(calls) == 2  # Retry Envelope 真重试
        assert inv.routing_target() is None  # 成功路径无降级
        assert len(container._envelopes) == 3 and len(container._salvages) == 1
