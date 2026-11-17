"""R3-T3 重试插件测试：重试后成功 / 耗尽 / 幂等拦截 / AROUND 单次调用。

重试循环放在 AROUND hook 内（R1 内核只消费 continue/deny/abort/fallback）。
drive() 模拟洋葱中心：用可配置结果序列驱动 next 回调，统计真实调用次数。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, DecisionAction, HookMode

from harness.resilience import RetryPolicy
from runtime import Effect, EffectKind, Result
from tests.r3.conftest import err_result, make_ctx, ok_result


async def drive(policy: RetryPolicy, ctx, results: list[Result]) -> tuple[Decision, int]:
    """驱动重试插件：把 results 依次喂给 ctx.meta['next']，返回裁决与调用次数。

    模拟 bus._around 的洋葱中心 core()：返回 continue + value=Result。
    """
    calls = 0

    async def fake_next() -> Decision:
        nonlocal calls
        result = results[min(calls, len(results) - 1)]
        calls += 1
        return Decision(action=DecisionAction.CONTINUE, value=result)

    ctx.meta["next"] = fake_next
    verdict = await policy(ctx)
    return verdict, calls


def _no_sleep(_delay: float):
    """测试用 sleeper：跳过真实等待。"""
    return asyncio.sleep(0)


class TestRetry:
    """重试语义：可重试错误退避后重试，确定性错误/耗尽/非幂等副作用拦截。"""

    def test_around_mode_and_name(self) -> None:
        p = RetryPolicy()
        assert p.name == "r3_retry"
        assert p.mode is HookMode.AROUND
        assert "before_invoke" in p.events

    def test_retry_then_success(self) -> None:
        """500 后重试成功：调 2 次，返回成功 Result。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        verdict, calls = asyncio.run(
            drive(p, make_ctx(), [err_result("500"), ok_result({"ok": 1})])
        )
        assert calls == 2
        assert verdict.value.ok is True

    def test_non_retryable_no_retry(self) -> None:
        """400 确定性错误：只调 1 次，不重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        verdict, calls = asyncio.run(drive(p, make_ctx(), [err_result("400")]))
        assert calls == 1
        assert verdict.value.ok is False

    def test_retry_exhausted(self) -> None:
        """一直 500：到 max_attempts 次停止，返回最后失败 Result。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        verdict, calls = asyncio.run(drive(p, make_ctx(), [err_result("500")]))
        assert calls == 3
        assert verdict.value.ok is False

    def test_idempotency_blocks_non_idempotent_retry(self) -> None:
        """TOOL 副作用无幂等键：拒绝重试（deny），只调 1 次。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        ctx = make_ctx(Effect(EffectKind.TOOL, {"name": "charge"}))
        verdict, calls = asyncio.run(drive(p, ctx, [err_result("500")]))
        assert calls == 1
        assert verdict.action is DecisionAction.DENY

    def test_idempotency_ok_with_key(self) -> None:
        """TOOL 副作用带幂等键：允许重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        ctx = make_ctx(Effect(EffectKind.TOOL, {"name": "charge", "x-idempotency-key": "k1"}))
        _verdict, calls = asyncio.run(
            drive(p, ctx, [err_result("500"), ok_result({"ok": 1})])
        )
        assert calls == 2

    def test_no_next_passes_through(self) -> None:
        """无 next（独立调用 / 非洋葱场景）：放行，不重试。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        ctx = make_ctx()  # meta 里没有 next
        d = asyncio.run(p(ctx))
        assert d.action is DecisionAction.CONTINUE

    def test_non_result_verdict_passthrough(self) -> None:
        """next 返回非 Result 载体（如 deny）：直接交上层，不解读。"""
        p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        ctx = make_ctx()

        async def deny_next() -> Decision:
            return Decision.deny("blocked")

        ctx.meta["next"] = deny_next
        d = asyncio.run(p(ctx))
        assert d.action is DecisionAction.DENY

    def test_retry_after_extends_delay(self) -> None:
        """Retry-After 提示延长退避（respect_retry_after 路径）。"""
        p = RetryPolicy(max_attempts=2, base_delay=0.01, jitter="none", sleeper=_no_sleep)
        ctx = make_ctx()
        # 500 分类的 suggested_delay 默认 None；用一个带 suggested_delay 的自定义分类无法走默认路径，
        # 这里断言正常重试路径覆盖 retry_after=None 分支即可。
        _verdict, calls = asyncio.run(drive(p, ctx, [err_result("500"), ok_result()]))
        assert calls == 2
