"""R7 新增方案 D4：大 tool 结果落盘而非截断（规格外新增设计决策）。

**决策**：工具结果超过阈值时，交给 BlobPort 落盘并回"store://<id>"句柄 + 摘要占位，
按需再读；**明确否决"截断"（truncate）**——截断会静默丢失工具结果尾部内容
（数据/错误/证据），属不可接受的语义损失，故 `mode="truncate"` 构造即 ValueError，
不留兼容层（对齐 D-10 纪律）。

**为何落盘而非截断**：
- 落盘保真：原文完整存于 BlobPort，摘要进上下文，模型按需 get 回读；
- 截断失真：尾部常被截掉的是最新数据/错误堆栈，恰恰最该被看到的部分；
- 小结果直接返回：低于阈值不落盘，避免无谓句柄开销（句柄本身也占 token）。

阈值是 R7 编排策略，属 ⑥组件；不放进 R1 Budgets（内核零领域语义）。
"""
from __future__ import annotations

from dataclasses import dataclass

from runtime import DynamicValue

from .offloader import BlobPort

__all__ = ["DEFAULT_OFFLOAD_THRESHOLD", "OffloadPolicy"]


# 默认阈值：8 KiB 字符数（默认 TokenCounter 口径为 Unicode 码点）。
# 具名常量，禁止裸魔法串（编码规则 5）。
DEFAULT_OFFLOAD_THRESHOLD = 8192


@dataclass(frozen=True, slots=True)
class OffloadPolicy:
    """卸载策略：超过阈值落盘，否则原样返回；截断模式被否决。

    字段：
        threshold: 触发落盘的大小（字符数）；必须为正。
        mode: 唯一合法值 "offload"；"truncate" 等一律拒绝（fail-closed）。
    """
    threshold: int = DEFAULT_OFFLOAD_THRESHOLD
    mode: str = "offload"

    def __post_init__(self) -> None:
        """校验阈值与模式（装配期 fail-closed，不把非法策略带进运行期）。"""
        if self.threshold <= 0:
            raise ValueError(f"offload threshold must be positive, got {self.threshold}")
        if self.mode != "offload":
            # 截断是被明确否决的方案（会静默丢数据），不给兼容开关。
            raise ValueError(
                f"mode={self.mode!r} rejected: truncation silently drops tool-result "
                "tail (data/errors/evidence); use offload-to-blob instead"
            )

    def should_offload(self, value: DynamicValue) -> bool:
        """是否超过阈值需要落盘（纯函数，无副作用）。"""
        return len(str(value)) > self.threshold

    async def apply(self, value: DynamicValue, store: BlobPort) -> DynamicValue:
        """应用策略：大值落盘回句柄，小值原样返回。

        Args:
            value: 工具结果值。
            store: 落盘后端（BlobPort）。
        Returns:
            大值 → "store://<id>" 句柄；小值 → 原值。绝不截断。
        """
        if not self.should_offload(value):
            return value
        return await store.put(value)
