"""R3-T2 熔断器三态机测试：closed→open→half_open→closed + 样本量 + 维度隔离。"""
from __future__ import annotations

import asyncio

from hooks import DecisionAction

from harness.resilience import BreakerStatus, CircuitBreaker
from tests.r3.conftest import err_result, make_ctx, ok_result

DIM = ("provider", "openai")


def _breaker(clock: object = None, **kw: object) -> CircuitBreaker:
    """构造熔断器；注入可控时钟推进冷却期。"""
    return CircuitBreaker(
        minimum_samples=3, window_size=10, failure_rate=0.5,
        clock=clock or (lambda: 0.0), **kw,  # type: ignore[arg-type]
    )


class TestCircuitBreaker:
    """三态机：样本不足不开、阈值触发 open、冷却后半开、探测成功回 closed。"""

    def test_sample_insufficient_never_opens(self) -> None:
        """样本 < minimum_samples：即使全失败也不 open（防误判）。"""
        b = _breaker()
        ctx = make_ctx()
        for _ in range(2):  # 2 < 3
            b.record(err_result("500"), ctx, dimension=DIM)
        assert b.state(DIM).status == BreakerStatus.CLOSED.value

    def test_closed_to_open_on_threshold(self) -> None:
        """样本足够 + 失败率达阈值 → OPEN。"""
        b = _breaker()
        ctx = make_ctx()
        for _ in range(3):
            b.record(err_result("500"), ctx, dimension=DIM)
        assert b.state(DIM).status == BreakerStatus.OPEN.value

    def test_open_intercepts_with_degrade(self) -> None:
        """OPEN 且冷却未满：before_invoke 返回 DEGRADE 拦截。"""
        now = [0.0]
        b = _breaker(clock=lambda: now[0], open_cooldown_s=30.0)
        ctx = make_ctx()
        for _ in range(3):
            b.record(err_result("500"), ctx, dimension=DIM)
        ctx.meta["breaker_dimension"] = DIM
        d = b.check(ctx)
        assert d.action is DecisionAction.DEGRADE

    def test_full_cycle(self) -> None:
        """完整周期：closed→open→(冷却)half_open→连续成功→closed。"""
        now = [0.0]
        b = _breaker(clock=lambda: now[0], open_cooldown_s=30.0, half_open_success_to_close=2)
        ctx = make_ctx()
        for _ in range(3):
            b.record(err_result("500"), ctx, dimension=DIM)
        assert b.state(DIM).status == BreakerStatus.OPEN.value

        now[0] = 31.0  # 冷却期届满
        b.maybe_half_open(DIM)
        assert b.state(DIM).status == BreakerStatus.HALF_OPEN.value

        b.record(ok_result(), ctx, dimension=DIM)
        b.record(ok_result(), ctx, dimension=DIM)
        assert b.state(DIM).status == BreakerStatus.CLOSED.value

    def test_half_open_probe_failure_reopens(self) -> None:
        """半开探测失败：立即重开 OPEN。"""
        now = [0.0]
        b = _breaker(clock=lambda: now[0], open_cooldown_s=30.0)
        ctx = make_ctx()
        for _ in range(3):
            b.record(err_result("500"), ctx, dimension=DIM)
        now[0] = 31.0
        b.maybe_half_open(DIM)
        b.record(err_result("500"), ctx, dimension=DIM)
        assert b.state(DIM).status == BreakerStatus.OPEN.value

    def test_count_false_does_not_trigger(self) -> None:
        """count=False（429 对方保护）不计入滑窗，不触发 open。"""
        b = _breaker()
        ctx = make_ctx()
        for _ in range(5):
            b.record(err_result("429"), ctx, dimension=DIM, count=False)
        assert b.state(DIM).status == BreakerStatus.CLOSED.value
        assert b.state(DIM).failures == 0

    def test_per_dimension_isolation(self) -> None:
        """维度隔离：一个 provider 故障不拖垮另一个。"""
        b = _breaker()
        ctx = make_ctx()
        for _ in range(3):
            b.record(err_result("500"), ctx, dimension=("provider", "openai"))
        assert b.state(("provider", "openai")).status == BreakerStatus.OPEN.value
        assert b.state(("provider", "anthropic")).status == BreakerStatus.CLOSED.value

    def test_closed_check_passes(self) -> None:
        """CLOSED 态 check 放行。"""
        b = _breaker()
        assert b.check(make_ctx()).action is DecisionAction.CONTINUE

    def test_half_open_probe_limit(self) -> None:
        """半开探测超额 → DEGRADE。"""
        now = [0.0]
        b = _breaker(clock=lambda: now[0], open_cooldown_s=30.0, half_open_max_probes=1)
        ctx = make_ctx()
        for _ in range(3):
            b.record(err_result("500"), ctx, dimension=DIM)
        now[0] = 31.0
        b.maybe_half_open(DIM)
        ctx.meta["breaker_dimension"] = DIM
        # 记一次半开成功探测（window 长度 +1）
        b.record(ok_result(), ctx, dimension=DIM, count=True)
        # 重新置为 HALF_OPEN 且 window 满:直接构造 store 状态
        from dataclasses import replace
        b._store[DIM] = replace(b._store[DIM], status=BreakerStatus.HALF_OPEN.value,
                                window=(True,), half_open_successes=0)
        assert b.check(ctx).action is DecisionAction.DEGRADE

    def test_per_dimension_disabled_merges(self) -> None:
        """per_dimension=False：所有维度归并到空键。"""
        b = _breaker(per_dimension=False)
        ctx = make_ctx()
        b.record(err_result("500"), ctx, dimension=("provider", "openai"))
        b.record(err_result("500"), ctx, dimension=("provider", "anthropic"))
        # 两次都归到空维度，openai 的故障会计入全局
        assert b.state(("", "")).failures == 2

    def test_call_hook_invoke_error_records(self) -> None:
        """__call__ 在 invoke_error 事件从 meta.last_result 记录失败。"""
        b = _breaker()
        ctx = make_ctx()
        ctx.event = "invoke_error"
        ctx.meta["last_result"] = err_result("500")
        asyncio.run(b(ctx))
        assert b.state(("", "")).failures == 1

    def test_call_hook_other_event_checks(self) -> None:
        """__call__ 在非 invoke_error 事件走 check 拦截。"""
        b = _breaker()
        ctx = make_ctx()
        ctx.event = "before_invoke"
        d = asyncio.run(b(ctx))
        assert d.action is DecisionAction.CONTINUE
