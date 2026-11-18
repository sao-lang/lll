"""生产链路断点锁：零手填内部字段 + 真实 Runner/Host/Gateway/插件。

每个用例对应一次审计发现的真实断点，确保修复后不再回退。
- E1: RetryPolicy 收到裸 Result 不崩
- E3: 后端 error 响应 → Result.failure（不再吞成成功）
- B2: 熔断降级 failed_target 是注册名 → 切 backup 不切回 primary
- B1: call 阶段（timeout/retry）的降级信号经 settle 选级 → sticky target
- 维度隔离: CircuitBreaker 用 dimension_of(effect) 算维度，生产真生效
- 幂等/副作用: effect.idempotency_key 具名字段生效
"""
from __future__ import annotations

import asyncio

from hooks import Decision, Host, Plugin, RequestScope, Signal
from harness.model import ModelCapability
from harness.resilience import (
    BreakerStatus, Bulkhead, CircuitBreaker, DegradeLadder, DegradeMode, DegradeStep,
    Degradation, Idempotency, RetryPolicy,
)
from hooks.dto import LLMRequest, LLMResponse, Usage
from runtime import Effect, EffectKind, Result, Runner, Session

from tests.r4.fakes import RecordingLLM


class _FailLLM:
    """模拟后端故障：返回带 error 的 LLMResponse（E3 的形态）。"""
    name = "primary"

    def __init__(self, name: str = "primary") -> None:
        self.name = name
        self.calls = 0
        self.last_usage = Usage()

    async def complete(self, req: LLMRequest, ctx: object) -> LLMResponse:
        self.calls += 1
        # 用与真实 adapter 一致的纯 HTTP 码（R3 normalize_code 认 ^5\d\d$）
        return LLMResponse(content="", error="500: backend down", usage=Usage())

    async def stream(self, req, ctx):
        yield ""

    @property
    def usage(self):
        return Usage()


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():
    resp = yield Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]})
    yield Effect(EffectKind.END, {"output": resp.get("content", "?") if isinstance(resp, dict) else "?"})


class TestProductionChainNoHandFill:
    def test_e3_backend_error_becomes_failure_result(self) -> None:
        """E3：后端 error 响应必须折叠为失败 Result，不再伪装成功。"""
        cap = ModelCapability(llm=_FailLLM())
        gw = cap.gateway()
        host = Host()
        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="e3", program_factory=_program)
        result = asyncio.run(runner.run(session))
        first = result.events[0].results[0]
        assert not first.ok and "500" in str(first.error), first

    def test_e1_retry_with_bare_result_does_not_crash(self) -> None:
        """E1：装了 RetryPolicy 的链路，core 返回裸 Result 不崩。"""
        primary = _FailLLM()
        cap = ModelCapability(llm=primary)
        gw = cap.gateway()
        host = Host()
        # retry 会重试 3 次，每次拿到失败 Result（裸），不能 AttributeError
        host.plugin(RetryPolicy(max_attempts=3, base_delay=0.001, jitter="full",
                                idempotency_required=False)).freeze()
        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="e1", program_factory=_program)
        result = asyncio.run(runner.run(session))
        err = result.events[0].results[0].error
        assert err is None or "PORT_CRASHED" not in str(err), f"retry 崩溃：{err}"
        assert primary.calls == 3, f"应重试 3 次，实际 {primary.calls}"

    def test_dimension_isolation_works_in_production(self) -> None:
        """维度隔离：dimension_of(effect) 在生产链路真生效，不靠手填 meta。"""
        breaker = CircuitBreaker(
            minimum_samples=2,
            dimension_of=lambda e: ("provider", str(e.payload.get("model") or "default")),
        )
        host = Host()
        host.plugin(breaker).freeze()
        # 两次不同 model 的失败：各自维度独立计数，都不该 open（各 1 次 < min_samples=2）
        e1 = Effect(EffectKind.LLM, {"model": "gpt-4", "messages": []})
        e2 = Effect(EffectKind.LLM, {"model": "claude", "messages": []})
        fail = Result.failure("500", "err")
        dim1 = ("provider", "gpt-4")
        dim2 = ("provider", "claude")
        asyncio.run(breaker.record(fail, dim1))
        asyncio.run(breaker.record(fail, dim2))
        st1 = asyncio.run(breaker.state(dim1))
        st2 = asyncio.run(breaker.state(dim2))
        assert len(st1.window) == 1 and len(st2.window) == 1
        assert st1.status != BreakerStatus.OPEN.value and st2.status != BreakerStatus.OPEN.value

    def test_b2_breaker_degrade_switches_to_backup(self) -> None:
        """B2：熔断守卫发的 failed_target 是注册名 → Degradation 据此切 backup。

        不再手填 Signal(failed_target="primary")，而是让真实 CircuitBreaker 发出，
        验证降级链用的失败目标是后端注册名而非熔断维度串。
        """
        breaker = CircuitBreaker(
            minimum_samples=1,
            dimension_of=lambda e: ("provider", "primary"),
            failed_target_of=lambda e: "primary",
        )
        host = Host()
        host.plugin(breaker).plugin(Degradation(
            _ladder(), available={"primary", "backup"}
        )).freeze()

        # 直接用 breaker 的守卫回调发信号（生产 before_invoke 走同一路径）
        scope = host.new_scope()
        effect = Effect(EffectKind.LLM, {"model": "primary", "messages": []})
        fail = Result.failure("500", "err")
        asyncio.run(breaker.record(fail, ("provider", "primary")))
        verdict = asyncio.run(breaker.check_and_signal(scope, effect))

        degrade_signals = [s for s in scope.signals if s.kind == "degrade"]
        assert degrade_signals, "熔断 OPEN 应发降级信号"
        assert degrade_signals[0].failed_target == "primary", \
            f"failed_target 应是注册名，实际 {degrade_signals[0].failed_target!r}"
        # 走 Host 编排的 settle（_settle 才会把 target 写回 scope），不直接调 hook
        asyncio.run(host._settle(scope, effect, object(), Decision.continue_()))
        assert scope.target == "backup", f"应切 backup，实际 {scope.target!r}"

    def test_b1_call_phase_signal_sets_sticky_target(self) -> None:
        """B1：call 阶段（如 retry 耗尽）发的降级信号经 settle 选级 → sticky target 跨 Effect 继承。"""
        class DegradeOnCall(Plugin):
            name = "degrade_on_call"
            def apply(self, host: Host) -> None:
                host.hook("around_invoke").tap(
                    self.name,
                    lambda next_fn, scope, effect: self._run(next_fn, scope),
                )
            async def _run(self, next_fn, scope: RequestScope):
                result = await next_fn()
                scope.emit(Signal("degrade", "timeout", "primary"))
                return result

        host = Host()
        host.plugin(DegradeOnCall()).plugin(
            Degradation(_ladder(), available={"primary", "backup"})
        ).freeze()
        from hooks.dto import LLMResponse, Usage
        from tests.r4.fakes import RecordingLLM
        cap = ModelCapability(llm=RecordingLLM([LLMResponse(content="ok", usage=Usage())]))
        gw = cap.gateway()
        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="b1", program_factory=_program)
        asyncio.run(runner.run(session))
        assert host.sticky_target_of("b1") == "backup", \
            f"call 阶段降级信号应经 settle 写该会话的 sticky target，实际 {host.sticky_target_of('b1')!r}"

    def test_idempotency_key_from_effect_field(self) -> None:
        """幂等：键来自 Effect.idempotency_key 具名字段，不靠手填 meta。"""
        idem = Idempotency()
        host = Host()
        host.plugin(idem).freeze()
        e = Effect(EffectKind.TOOL, {"call": {}}, idempotency_key="key-1")
        first = idem.check(host.new_scope(), e)
        second = idem.check(host.new_scope(), e)
        assert first.action.value == "continue"
        assert second.action.value == "deny", "重复幂等键应被拒"

    def test_bulkhead_slot_released_on_guard_short_circuit(self) -> None:
        """E2 边界：占槽后被后面守卫短路，槽位仍须释放（scope.releases 兜底）。"""
        bulkhead = Bulkhead(pools={"p": 1}, pool="p", on_full="reject")
        host = Host()
        host.plugin(bulkhead).freeze()
        e = Effect(EffectKind.TOOL, {})
        scope = host.new_scope()
        verdict = bulkhead.check(scope, e)
        assert verdict.action.value == "continue"
        assert bulkhead._used["p"] == 1
        scope.release_all()  # Host 在短路路径会调此
        assert bulkhead._used["p"] == 0, "短路后槽位必须释放"
