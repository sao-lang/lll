"""R7 新增方案 D5：KV cache 命中率作为一等可观测指标。

**决策**：KV cache 的 hit/miss 不是零散 trace 字符串，而是具名 DTO（`CacheTelemetry`），
提供一等字段 `hit_rate`（hits / total），可进 R10 trace、可按维度标签归因
（如 "llm:primary"、"prefix:system"）。

**为何是一等**：
- 可断言：结构化字段可被单测/门禁直接断言，不是"从日志里 grep"；
- 可归因：label 维度区分不同模型/前缀的命中率，定位缓存失效根因；
- 安全默认：无访问时 hit_rate=0.0（不是 NaN/None），trace 序列化不炸；
- 纯函数：record_hit/record_miss 返回新实例，可重放（对齐 R7 §12.5 可重放纪律）。

本模块是 ⑥组件（观测数据载体），不进 PORT_PROTOCOLS（Port 数量不变）。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CacheTelemetry"]


@dataclass(frozen=True, slots=True)
class CacheTelemetry:
    """KV cache 命中率一等观测指标（不可变、可重放）。

    字段：
        hits: 命中次数。misses: 未命中次数。
        label: 维度标签（模型/前缀等），缺省 "default" 便于归因。
    """
    hits: int = 0
    misses: int = 0
    label: str = "default"

    def __post_init__(self) -> None:
        """计数不得为负（fail-closed）。"""
        if self.hits < 0 or self.misses < 0:
            raise ValueError("cache hit/miss counts must be non-negative")

    @property
    def total(self) -> int:
        """总访问次数（hits + misses）。"""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """命中率（hits / total）；无访问时为 0.0，绝不产生 NaN/None。"""
        if self.total == 0:
            return 0.0
        return self.hits / self.total

    def record_hit(self) -> CacheTelemetry:
        """记录一次命中，返回新实例（纯函数，不改原对象）。"""
        return CacheTelemetry(hits=self.hits + 1, misses=self.misses, label=self.label)

    def record_miss(self) -> CacheTelemetry:
        """记录一次未命中，返回新实例。"""
        return CacheTelemetry(hits=self.hits, misses=self.misses + 1, label=self.label)

    def with_label(self, label: str) -> CacheTelemetry:
        """换维度标签，返回新实例（保留计数）。"""
        return CacheTelemetry(hits=self.hits, misses=self.misses, label=label)
