"""R7 新增方案测试：
- 方案 D4：大 tool 结果落盘而非截断（OffloadPolicy 阈值判定 + 否决截断）。
- 方案 D5：KV cache 命中率作为一等可观测指标（CacheTelemetry）。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import DynamicValue


# ---------- 方案 D4：大结果落盘而非截断 ----------
def test_offload_policy_defers_small_value_without_touching_blob() -> None:
    """低于阈值的小结果原样返回，不落盘（避免无谓句柄开销）。"""
    from harness.context.offload_policy import DEFAULT_OFFLOAD_THRESHOLD, OffloadPolicy
    from harness.context.offloader import BlobPort, InMemoryBlobStore

    class SpyStore(InMemoryBlobStore):
        puts = 0

        async def put(self, value: DynamicValue) -> str:  # type: ignore[override]
            self.puts += 1
            return await super().put(value)

    async def run() -> None:
        store = SpyStore()
        policy = OffloadPolicy(threshold=DEFAULT_OFFLOAD_THRESHOLD)
        result = await policy.apply("small", store)
        # 小值原样返回（非 store:// 句柄），且未写 blob
        assert not str(result).startswith("store://")
        assert store.puts == 0

    asyncio.run(run())


def test_offload_policy_offloads_large_value_to_blob() -> None:
    """超阈值的大结果落盘，返回可读句柄（不截断）。"""
    from harness.context.offload_policy import OffloadPolicy
    from harness.context.offloader import InMemoryBlobStore

    async def run() -> None:
        store = InMemoryBlobStore()
        policy = OffloadPolicy(threshold=10)
        big = "x" * 100
        handle = await policy.apply(big, store)
        assert str(handle).startswith("store://")
        # 关键：落盘后内容完整可读，未被截断
        assert await store.get(str(handle)) == big

    asyncio.run(run())


def test_offload_policy_rejects_truncate_mode() -> None:
    """方案红线：截断模式被显式否决，构造即 ValueError（不留兼容层）。"""
    from harness.context.offload_policy import OffloadPolicy

    with pytest.raises(ValueError, match="truncate"):
        OffloadPolicy(threshold=10, mode="truncate")  # type: ignore[arg-type]


def test_offload_policy_threshold_must_be_positive() -> None:
    """阈值须为正（fail-closed），0/负数拒绝。"""
    from harness.context.offload_policy import OffloadPolicy

    with pytest.raises(ValueError):
        OffloadPolicy(threshold=0)
    with pytest.raises(ValueError):
        OffloadPolicy(threshold=-1)


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
