"""R3-T1 核心类型契约测试：失败分类 / 退避 / 降级阶梯 / 维度键 / 熔断状态。

这些是 R3 的共享设施（重试与熔断共用一次分类），全部为纯函数/不可变值对象，
不触碰 IO。先写测试（RED），再实现（GREEN）。
"""
from __future__ import annotations

from random import Random

import pytest

from harness.resilience import (
    DEFAULT_FAILURE_TABLE,
    BreakerState,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    DimensionKey,
    FailureClass,
    backoff_delay,
    classify_failure,
    normalize_code,
    validate_ladder,
    validate_retry_spec,
)
from tests.r3.conftest import default_spec, err_result, fixed_rng, make_ctx, ok_result


class TestFailureClassification:
    """默认失败分类表：区分「重试」与「计入熔断」两种语义。"""

    def test_rate_limited_retries_without_breaker(self) -> None:
        """429 重试但不计入熔断（对方正常保护，不是宕机）。"""
        fc = classify_failure(err_result("429"), make_ctx())
        assert fc.retryable is True
        assert fc.count_toward_breaker is False

    def test_server_error_retries_and_counts(self) -> None:
        """5xx / TIMEOUT / CONN_RESET 重试且计入熔断。"""
        for code in ("500", "502", "503", "TIMEOUT", "CONN_RESET"):
            fc = classify_failure(err_result(code), make_ctx())
            assert fc.retryable is True, code
            assert fc.count_toward_breaker is True, code

    def test_client_error_no_retry(self) -> None:
        """400/401/403/PORT_MISSING 确定性错误：不重试、不计熔断。"""
        for code in ("400", "401", "403", "PORT_MISSING"):
            fc = classify_failure(err_result(code), make_ctx())
            assert fc.retryable is False, code
            assert fc.count_toward_breaker is False, code

    def test_schema_invalid_retries_without_breaker(self) -> None:
        """SCHEMA_INVALID 重试但不计熔断（可能模型偶发抖动）。"""
        fc = classify_failure(err_result("SCHEMA_INVALID"), make_ctx())
        assert fc.retryable is True
        assert fc.count_toward_breaker is False

    def test_success_is_terminal(self) -> None:
        """成功为终态：不重试、不计熔断。"""
        fc = classify_failure(ok_result({"x": 1}), make_ctx())
        assert fc.retryable is False
        assert fc.count_toward_breaker is False

    def test_normalize_code_http_and_symbolic(self) -> None:
        """HTTP 码与符号码归一到同一族。"""
        assert normalize_code("429") == normalize_code("TOO_MANY_REQUESTS")
        assert normalize_code("503") == "SERVER_ERROR"
        assert normalize_code("  500  ") == "SERVER_ERROR"  # 去空白+大写
        assert normalize_code("WEIRD_VENDOR_CODE") == "WEIRD_VENDOR_CODE"  # 未知原样

    def test_unknown_code_respects_port_retryable_flag(self) -> None:
        """未知码尊重 PortError.retryable 显式标记，但保守不计熔断。"""
        fc = classify_failure(err_result("WEIRD", retryable=True), make_ctx())
        assert fc.retryable is True
        assert fc.count_toward_breaker is False

    def test_default_table_keys_complete(self) -> None:
        """默认表覆盖所有语义族，便于装配期校验注入方覆盖完整。"""
        expected = {
            "RATE_LIMITED", "SERVER_ERROR", "TIMEOUT", "CONN_RESET",
            "BAD_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "PORT_MISSING",
            "SCHEMA_INVALID",
        }
        assert expected == set(DEFAULT_FAILURE_TABLE)

    def test_custom_classifier_injected(self) -> None:
        """注入自定义分类器时委托给它，返回其结论。"""
        sentinel = FailureClass(True, True, 1.5, "CUSTOM")

        class Custom:
            name = "custom"

            def classify(self, result: object, ctx: object) -> FailureClass:
                return sentinel

        fc = classify_failure(err_result("500"), make_ctx(), classifier=Custom())
        assert fc == sentinel


class TestBackoff:
    """退避算法：纯函数 + 随机源显式注入（可回放）。"""

    def test_none_is_raw_capped(self) -> None:
        """jitter=none：返回 base×mult^(n-1)，并受 max_delay 封顶。"""
        spec = default_spec(base_delay=1.0, multiplier=2.0, max_delay=10.0, jitter="none")
        assert backoff_delay(1, spec, fixed_rng()) == pytest.approx(1.0)
        assert backoff_delay(2, spec, fixed_rng()) == pytest.approx(2.0)
        assert backoff_delay(3, spec, fixed_rng()) == pytest.approx(4.0)
        assert backoff_delay(10, spec, fixed_rng()) == pytest.approx(10.0)  # 封顶

    def test_full_is_uniform_zero_to_raw(self) -> None:
        """full：落在 [0, raw] 区间，多次结果不同（有抖动）。"""
        spec = default_spec(base_delay=1.0, multiplier=1.0, jitter="full")
        vals = [backoff_delay(1, spec, Random(i)) for i in range(20)]
        assert all(0.0 <= v <= 1.0 for v in vals)
        assert len(set(vals)) > 1  # 确实抖动了

    def test_equal_is_half_plus_half(self) -> None:
        """equal：落在 [raw/2, raw] 区间。"""
        spec = default_spec(base_delay=2.0, multiplier=1.0, jitter="equal")
        vals = [backoff_delay(1, spec, Random(i)) for i in range(20)]
        assert all(1.0 <= v <= 2.0 for v in vals)

    def test_decorrelated_uses_previous(self) -> None:
        """decorrelated：基于 prev_delay×uniform(1,3)，prev=0 时回退到 base_delay。"""
        spec = default_spec(base_delay=1.0, max_delay=100.0, jitter="decorrelated")
        no_prev = backoff_delay(1, spec, Random(0), prev_delay=0.0)
        with_prev = backoff_delay(1, spec, Random(0), prev_delay=10.0)
        assert 1.0 <= no_prev <= 3.0
        assert 10.0 <= with_prev <= 30.0

    def test_retry_after_takes_max(self) -> None:
        """服务端 Retry-After：取 max(退避, 服务端提示)。"""
        spec = default_spec(base_delay=0.1, jitter="none", respect_retry_after=True)
        assert backoff_delay(1, spec, fixed_rng(), retry_after=5.0) == pytest.approx(5.0)

    def test_retry_after_ignored_when_disabled(self) -> None:
        """respect_retry_after=False 时忽略服务端提示。"""
        spec = default_spec(base_delay=0.1, jitter="none", respect_retry_after=False)
        assert backoff_delay(1, spec, fixed_rng(), retry_after=5.0) == pytest.approx(0.1)

    def test_zero_base_with_none_is_rejected(self) -> None:
        """装配期校验：base_delay=0 且 jitter=none 会形成重试风暴，拒绝。"""
        spec = default_spec(base_delay=0.0, jitter="none")
        with pytest.raises(ValueError):
            validate_retry_spec(spec)

    def test_invalid_params_rejected(self) -> None:
        """max_attempts<1 / multiplier<1 装配期拒绝。"""
        with pytest.raises(ValueError):
            validate_retry_spec(default_spec(max_attempts=0))
        with pytest.raises(ValueError):
            validate_retry_spec(default_spec(multiplier=0.5))


class TestDegradeLadder:
    """降级阶梯：无环 + 末级拒答 + quality 严格递减（R3 红线）。"""

    def _step(self, name: str, quality: float, mode: DegradeMode = DegradeMode.REROUTE) -> DegradeStep:
        return DegradeStep(target=name, mode=mode, quality=quality)

    def test_valid_ladder(self) -> None:
        """合法阶梯通过校验。"""
        ladder = DegradeLadder(steps=(
            self._step("primary", 1.0),
            self._step("backup", 0.7),
            self._step("cheap", 0.4),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        validate_ladder(ladder)  # 不抛异常

    def test_cycle_rejected(self) -> None:
        """重复 target（成环）装配期拒绝。"""
        ladder = DegradeLadder(steps=(
            self._step("primary", 1.0),
            self._step("primary", 0.5),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        with pytest.raises(ValueError, match="cycle"):
            validate_ladder(ladder)

    def test_no_refuse_fallback_rejected(self) -> None:
        """末级非拒答：禁止编造，装配期拒绝。"""
        ladder = DegradeLadder(steps=(self._step("primary", 1.0),))
        with pytest.raises(ValueError, match="REFUSE"):
            validate_ladder(ladder)

    def test_quality_not_decreasing_rejected(self) -> None:
        """quality 必须严格递减。"""
        ladder = DegradeLadder(steps=(
            self._step("primary", 0.5),
            self._step("backup", 0.5),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        with pytest.raises(ValueError, match="decrease"):
            validate_ladder(ladder)

    def test_pick_best_available(self) -> None:
        """从可用集合选 quality 最高的一级；拒答总可用。"""
        ladder = DegradeLadder(steps=(
            self._step("primary", 1.0),
            self._step("backup", 0.7),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        assert ladder.pick({"primary", "backup"}).target == "primary"
        assert ladder.pick({"backup"}).target == "backup"
        assert ladder.pick(set()).mode is DegradeMode.REFUSE


class TestDimensionAndState:
    """维度键与熔断状态的不可变值语义。"""

    def test_dimension_key_as_tuple(self) -> None:
        key = DimensionKey(dim_type="provider", name="openai")
        assert key.as_tuple() == ("provider", "openai")

    def test_breaker_state_failure_rate(self) -> None:
        empty = BreakerState()
        assert empty.failure_rate() == 0.0
        windowed = BreakerState(status="closed", failures=2, window=(True, False, True, False))
        assert windowed.failure_rate() == pytest.approx(0.5)
