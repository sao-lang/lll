"""R7 测试：KV cache 命中率作为一等可观测指标（CacheTelemetry）。

（OffloadPolicy 已按源码零残留纪律删除：生产代码 0 消费点，B10 决议"删死壳或接线"）
"""
from __future__ import annotations

import pytest

# ---------- 方案 D5：KV cache 命中率一等可观测指标 ----------
def test_cache_telemetry_hit_rate() -> None:
    """CacheTelemetry 记录 hit/miss，hit_rate 为一等值（非推导注释）。"""
    from harness.context.observability import CacheTelemetry

    telemetry = CacheTelemetry(hits=3, misses=1)
    assert telemetry.hits == 3
    assert telemetry.misses == 1
    assert telemetry.total == 4
    assert telemetry.hit_rate == pytest.approx(0.75)


def test_cache_telemetry_empty_is_zero_not_nan() -> None:
    """无访问时 hit_rate=0.0（不产生 NaN/None，trace 安全）。"""
    from harness.context.observability import CacheTelemetry

    t = CacheTelemetry()
    assert t.total == 0
    assert t.hit_rate == 0.0


def test_cache_telemetry_record_hit_miss_immutable() -> None:
    """record 返回新实例（纯函数、可重放），不改原对象。"""
    from harness.context.observability import CacheTelemetry

    t0 = CacheTelemetry()
    t1 = t0.record_hit()
    t2 = t1.record_miss()
    assert (t0.hits, t0.misses) == (0, 0)
    assert (t1.hits, t1.misses) == (1, 0)
    assert (t2.hits, t2.misses) == (1, 1)


def test_cache_telemetry_label_is_dimension_key() -> None:
    """指标带维度标签（如 model/prefix），便于归因；标签为空时归因到 default。"""
    from harness.context.observability import CacheTelemetry

    t = CacheTelemetry(label="llm:primary").record_hit()
    assert t.label == "llm:primary"
    assert CacheTelemetry().label == "default"


def test_manager_exposes_cache_telemetry_as_first_class() -> None:
    """ContextMemoryManager 暴露 cache_telemetry 一等观测面，可记录 KV cache 命中。"""
    from harness.context.manager import ContextMemoryManager
    from harness.context.observability import CacheTelemetry

    mgr = ContextMemoryManager()
    assert isinstance(mgr.cache_telemetry, CacheTelemetry)
    mgr.record_cache_hit("llm:primary")
    assert mgr.cache_telemetry.hit_rate == 1.0
