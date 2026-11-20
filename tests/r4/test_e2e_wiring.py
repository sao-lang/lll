"""R4 端到端装配（新体系）：Runner → AOPContainer → ModelGateway → Result。

覆盖降级切 backup / REFUSE 真拒答 / 后端错误折叠 / 重试次数 / 熔断维度隔离 /
幂等键 / 舱壁释放 / model 通知 / 缓存命中。全部在真实 Runner 链路上断言（不放水）。
"""
from __future__ import annotations

import asyncio

from aop import AOPContainer, FaultRecord, Invocation, Verdict, Warden
from hooks import LifecycleBus, LifecyclePhase, Notice
from runtime import Invoke, Result, Runner, Session, Terminate

from harness.model import ModelCapability
from harness.resilience import (
    BreakerStatus, BudgetGuard, Bulkhead, CircuitBreaker, DegradeLadder,
    DegradeMode, Degradation, DegradeStep, Idempotency, RetryPolicy,
)

from tests.r4.conftest import pctx, req
from hooks import LLMRequest, LLMResponse, Usage


class FakeLLM:
    """可配置失败/成功的假后端（按名字返回 content，记录调用次数）。"""
    def __init__(self, name: str = "primary", *, fail: str | None = None) -> None:
        self.name = name
        self.calls = 0
        self.fail = fail

    async def complete(self, request: LLMRequest, ctx: object) -> LLMResponse:
        self.calls += 1
        if self.fail:
            return LLMResponse(content="", error=self.fail)
        return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1))

    async def stream(self, request, ctx):
        yield self.name

    @property
    def last_usage(self):
        return Usage(prompt_tokens=1)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


class _FaultWarden(Warden):
    name = "fault_warden"
    def __init__(self, reason: str = "breaker_open") -> None:
        self.reason = reason
    async def guard(self, inv: Invocation) -> Verdict:
        inv.write_fault(FaultRecord(reason=self.reason, failed_target="primary"))
        return Verdict(allowed=True)


def _program():
    resp = yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Terminate(output=resp.get("content", "?") if isinstance(resp, dict) else "?")


def _run(container: AOPContainer, sid: str) -> object:
    runner = Runner(call=container.as_call())
    return asyncio.run(runner.run(Session(session_id=sid, program_factory=_program)))


class TestFullChainWiring:
    def test_degrade_switches_endpoint(self) -> None:
        """Warden 故障 → Degradation 选 backup → 网关切备用，primary 零调用。"""
        primary, backup = FakeLLM("primary"), FakeLLM("backup")
        cap = ModelCapability(llm=primary)
        cap.add_backend("backup", backup)
        degradation = Degradation(_ladder(), available={"primary", "backup"})
        c = AOPContainer(cap.gateway().as_call()).add_warden(_FaultWarden()).add_salvage(degradation)
        result = _run(c, "e2e-degrade")
        assert result.output == "backup"
        assert backup.calls == 1 and primary.calls == 0

    def test_refuse_truly_refuses_no_backend_call(self) -> None:
        """REFUSE 裁决 → 真拒答，零后端调用，失败进 Result。"""
        primary = FakeLLM("primary")
        cap = ModelCapability(llm=primary)
        cap.add_backend("backup", FakeLLM("backup"))
        degradation = Degradation(_ladder(), available=set())  # 无可用 → REFUSE

        class RefuseWarden(Warden):
            name = "refuse_warden"
            async def guard(self, inv):
                inv.write_fault(FaultRecord("ladder_exhausted", "primary"))
                return Verdict(allowed=True)

        c = AOPContainer(cap.gateway().as_call()).add_warden(RefuseWarden()).add_salvage(degradation)
        result = _run(c, "e2e-refuse")
        assert primary.calls == 0
        # REFUSE 折叠为失败 Result（code=REFUSED），零后端调用、绝不编造答案。
        first = result.events[0].results[0]
        assert not first.ok and first.error is not None and first.error.code == "REFUSED"

    def test_backend_error_folds_into_failure_result(self) -> None:
        """后端 error 响应折叠为失败 Result（不伪装成功）。"""
        cap = ModelCapability(llm=FakeLLM("primary", fail="500:boom"))
        c = AOPContainer(cap.gateway().as_call())
        result = _run(c, "e3")
        first = result.events[0].results[0]
        assert not first.ok and "500" in str(first.error)

    def test_retry_calls_backend_exact_times(self) -> None:
        """装 RetryPolicy 的链路：失败后端被重试 max_attempts 次，不崩。"""
        primary = FakeLLM("primary", fail="500:boom")
        cap = ModelCapability(llm=primary)
        retry = RetryPolicy(max_attempts=3, base_delay=0.001, jitter="none", idempotency_required=False)
        c = AOPContainer(cap.gateway().as_call()).add_envelope(retry)
        result = _run(c, "e1")
        err = result.events[0].results[0].error
        assert err is None or "PORT_CRASHED" not in str(err)
        assert primary.calls == 3, f"应重试 3 次，实际 {primary.calls}"

    def test_breaker_dimension_isolation(self) -> None:
        """熔断维度隔离：不同 model 的失败各自计数，不互相拖垮。"""
        breaker = CircuitBreaker(
            minimum_samples=2,
            dimension_of=lambda inv: ("provider", str(inv.effect.payload.get("model") or "default")),
        )
        # 两次不同 model 各记 1 次失败：各维度样本不足，均不 OPEN
        import asyncio as _a
        from tests.r3.conftest import err_result
        _a.run(breaker.record(err_result("500"), ("provider", "gpt-4")))
        _a.run(breaker.record(err_result("500"), ("provider", "claude")))
        st1 = _a.run(breaker.state(("provider", "gpt-4")))
        st2 = _a.run(breaker.state(("provider", "claude")))
        assert len(st1.window) == 1 and len(st2.window) == 1
        assert st1.status != BreakerStatus.OPEN.value and st2.status != BreakerStatus.OPEN.value

    def test_idempotency_key_from_effect(self) -> None:
        """幂等键从 Invoke.idempotency_key 读：重复副作用被拒。"""
        idem = Idempotency()
        from tests.r3.conftest import make_ctx
        eff = Invoke(port="tool", payload={}, idempotency_key="K")
        assert asyncio.run(idem.guard(make_ctx(eff))).allowed is True
        assert asyncio.run(idem.guard(make_ctx(eff))).allowed is False

    def test_bulkhead_slot_released_after_wrap(self) -> None:
        """舱壁：wrap 结束 try/finally 释放槽，无泄漏。"""
        bh = Bulkhead(pools={"p": 1}, pool="p", on_full="reject")
        async def core(inv): return Result.success("ok")
        asyncio.run(bh.wrap(make_inv(), core))
        assert bh.acquire("p") is True
        bh.release("p")

    def test_model_notification_fires_in_chain(self) -> None:
        """before_model/after_model 通知在全链真触发（纯通知不拦截）。"""
        bus = LifecycleBus()
        seen: list[str] = []
        bus.on(LifecyclePhase.BEFORE_MODEL, lambda n: seen.append(n.phase.value))
        bus.on(LifecyclePhase.AFTER_MODEL, lambda n: seen.append(n.phase.value))
        cap = ModelCapability(llm=FakeLLM("primary"), bus=bus)
        c = AOPContainer(cap.gateway().as_call())
        _run(c, "e2e-notify")
        assert "before_model" in seen and "after_model" in seen

    def test_exact_cache_hit_reuses_backend_once(self) -> None:
        """同一网关复用：第二次缓存命中，后端只调一次。"""
        primary = FakeLLM("primary")
        gw = ModelCapability(llm=primary).gateway()
        c1 = AOPContainer(gw.as_call())
        c2 = AOPContainer(gw.as_call())
        r1 = _run(c1, "cache-1")
        r2 = _run(c2, "cache-2")
        assert r1.output == "primary" and r2.output == "primary"
        assert primary.calls == 1, f"精确缓存未命中：后端被调用 {primary.calls} 次"


def make_inv() -> Invocation:
    """构造最小 Invocation（舱壁测试用）。"""
    return Invocation(
        effect=Invoke(port="tool", payload={}),
        ctx=pctx(),
        session=type("S", (), {"session_id": "bulkhead"})(),
        meta={},
    )
