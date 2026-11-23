"""R3 能力装配测试：ResilienceCapability.wrap(core) 产出具名领域执行器。

插件保留一等身份（name + 实例状态 + 构造注入），install 按领域把策略注入
ReliabilityExecutor 的具名字段；缺席即跳过，无角色分派与槽位列表。
extra_plugins 只接受函数式 (CallFn)->CallFn 变换器。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from compose.context import Invocation
from runtime import Invoke, Result

from harness.resilience import (
    Bulkhead, CircuitBreaker, DegradeLadder, DegradeMode, DegradeStep,
    Degradation, Idempotency, KillSwitch, RateLimit, ResilienceCapability,
    ReliabilityExecutor, RetryPolicy, Timeout, Watchdog,
)
from tests.r3.conftest import err_result, make_ctx


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _names(plugins: tuple[object, ...]) -> list[str]:
    return [str(getattr(p, "name", "")) for p in plugins]


def _ok_core(data: object = "ok") -> Callable[[Invocation], Awaitable[Result]]:
    """最小领域 core：直接成功返回（带可选 data）。"""
    async def core(inv: Invocation) -> Result:
        return Result.success(data)
    return core


class TestCapabilityAssembly:
    def test_no_args_installs_nothing(self) -> None:
        """opt-in：不传插件 → 零策略；core 仍可执行。"""
        cap = ResilienceCapability()
        assert cap.plugins() == ()
        ex = cap.wrap(_ok_core("x"))
        assert (ex._kill_switch, ex._watchdog, ex._idempotency, ex._rate_limit,
                ex._bulkhead, ex._breaker, ex._retry, ex._timeout, ex._degradation) == (None,) * 9
        result = asyncio.run(ex.run_invocation(make_ctx()))
        assert result.ok and result.data == "x"

    def test_explicit_builtin_registered_by_domain_field(self) -> None:
        """只装传入插件，按领域注入执行器具名字段。"""
        kill = KillSwitch(flag_store={})
        retry = RetryPolicy()
        cap = ResilienceCapability(killswitch=kill, retry=retry)
        assert _names(cap.plugins()) == ["r3_killswitch", "r3_retry"]
        ex = cap.wrap(_ok_core("x"))
        assert ex._kill_switch is kill and ex._retry is retry
        assert ex._degradation is None and ex._breaker is None

    def test_all_plugins_by_domain_field(self) -> None:
        """全插件装配：九领域策略各有具名字段，顺序确定。"""
        kill = KillSwitch(flag_store={})
        watchdog = Watchdog()
        idem = Idempotency()
        limit = RateLimit()
        bulkhead = Bulkhead()
        breaker = CircuitBreaker()
        retry = RetryPolicy()
        timeout = Timeout()
        deg = Degradation(_ladder())
        cap = ResilienceCapability(
            killswitch=kill, watchdog=watchdog, idempotency=idem,
            rate_limit=limit, bulkhead=bulkhead, breaker=breaker,
            retry=retry, timeout=timeout, degradation=deg,
        )
        ex = cap.wrap(_ok_core("x"))
        assert isinstance(ex, ReliabilityExecutor)
        assert ex.registry is None
        assert set(_names(cap.plugins())) == {
            "r3_killswitch", "r3_watchdog", "r3_idempotency", "r3_ratelimit",
            "r3_bulkhead", "r3_breaker", "r3_retry", "r3_timeout", "r3_degrade",
        }
        assert ex._kill_switch is kill and ex._watchdog is watchdog
        assert ex._idempotency is idem and ex._rate_limit is limit
        assert ex._bulkhead is bulkhead and ex._breaker is breaker
        assert ex._retry is retry and ex._timeout is timeout and ex._degradation is deg
        # 策略身份按领域落位；原角色分组断言在去角色化后无等价物，已换成字段断言。

    def test_killswitch_rejects_without_calling_core(self) -> None:
        """killswitch 置位 → 终止拒绝（原 guard allowed=False 的领域等价）。"""
        kill = KillSwitch(flag_store={"kill:global": True})
        called: list[int] = []

        async def core(inv: object) -> Result:
            called.append(1)
            return Result.success("should-not-run")

        ex = ResilienceCapability(killswitch=kill).wrap(core)
        result = asyncio.run(ex.run_invocation(make_ctx()))
        assert not result.ok and result.error is not None
        assert result.error.code == "INVOKE_REJECTED"
        assert result.error.message == "killswitch"
        assert called == []


class TestCustomPlugins:
    def test_custom_degradation_injected(self) -> None:
        """注入自定义 Degradation 实例：插件名与执行器字段均保留实例身份。"""
        class CustomDegradation(Degradation):
            name = "custom_degrade"

        custom = CustomDegradation(_ladder())
        cap = ResilienceCapability(degradation=custom)
        ex = cap.wrap(_ok_core("x"))
        assert _names(cap.plugins()) == ["custom_degrade"]
        assert ex._degradation is custom

    def test_no_args_installs_no_defaults(self) -> None:
        """不传即空：不隐式装默认插件。"""
        assert ResilienceCapability().plugins() == ()


class TestExtraPlugins:
    def test_extra_transformer_applied(self) -> None:
        """extra_plugins 只收函数式变换器：前后包裹生效。"""
        calls: list[str] = []
        type _InvCall = Callable[[Invocation], Awaitable[Result]]

        def transform(nxt: _InvCall) -> _InvCall:
            async def wrapped(inv: Invocation) -> Result:
                calls.append("before")
                result = await nxt(inv)
                calls.append("after")
                return result
            return wrapped

        cap = ResilienceCapability(extra_plugins=(transform,))  # type: ignore[arg-type]
        assert transform in cap.plugins()
        ex = cap.wrap(_ok_core("x"))
        result = asyncio.run(ex.run_invocation(make_ctx()))
        assert result.ok and calls == ["before", "after"]

    def test_extra_plugin_in_plugins(self) -> None:
        """extra_plugins 原样进 plugins()。"""
        def transform(nxt: object) -> object:
            return nxt

        assert transform in ResilienceCapability(extra_plugins=(transform,)).plugins()  # type: ignore[arg-type]

    def test_non_callable_extra_rejected(self) -> None:
        """装配期 fail-closed：非变换器扩展被拒绝。"""
        with pytest.raises(ValueError, match="call transformer"):
            ResilienceCapability(extra_plugins=(object(),))  # type: ignore[arg-type]


class TestExecutorBreakerDomain:
    """从角色方法测试迁来的熔断领域断言（断言不放水，只换宿主）。"""

    def test_open_breaker_writes_fault_with_registered_target(self) -> None:
        """熔断 OPEN → 写 fault（reason + failed_target 注册名，B2）。"""
        now = [0.0]
        dim = ("provider", "openai")
        cb = CircuitBreaker(
            minimum_samples=3, clock=lambda: now[0], open_cooldown_s=30.0,
            dimension_of=lambda inv: dim,
        )
        for _ in range(3):
            asyncio.run(cb.record(err_result("500"), dim))
        inv = make_ctx(Invoke(port="llm", payload={"model": "primary"}))
        ex = ResilienceCapability(breaker=cb).wrap(_ok_core("x"))
        result = asyncio.run(ex.run_invocation(inv))
        fault = inv.fault()
        assert result.ok
        assert fault is not None and fault.reason == "breaker_open"
        assert fault.failed_target == "primary"

    def test_observe_records_success_result(self) -> None:
        """调用成功后熔断记数：无失败。"""
        cb = CircuitBreaker(minimum_samples=3)
        ex = ResilienceCapability(breaker=cb).wrap(_ok_core())
        result = asyncio.run(ex.run_invocation(make_ctx()))
        assert result.ok
        assert asyncio.run(cb.state(())).failures == 0

    def test_observe_records_exception_as_failure_and_reraises(self) -> None:
        """中心抛异常按失败记数并原样抛回。"""
        cb = CircuitBreaker(minimum_samples=3)

        async def boom(i: object) -> Result:
            raise RuntimeError("boom")

        ex = ResilienceCapability(breaker=cb).wrap(boom)
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(ex.run_invocation(make_ctx()))
        assert asyncio.run(cb.state(())).failures == 1


class TestDomainComposition:
    def test_bulkhead_retry_timeout_degradation_compose(self) -> None:
        """多领域策略（舱壁/重试/超时/降级）在执行器中组合执行无断点。"""
        cap = ResilienceCapability(
            bulkhead=Bulkhead(pools={"default": 5}),
            retry=RetryPolicy(max_attempts=2, base_delay=0.001, jitter="none"),
            timeout=Timeout(timeout=5.0),
            degradation=Degradation(_ladder(), available={"primary", "backup"}),
        )
        calls: list[int] = []

        async def core(inv: object) -> Result:
            calls.append(1)
            return Result.success("ok") if len(calls) >= 2 else Result.failure("500", "transient")

        ex = cap.wrap(core)
        inv = make_ctx()
        result = asyncio.run(ex.run_invocation(inv))
        assert result.ok and result.data == "ok"
        assert len(calls) == 2
        assert inv.routing_target() is None
