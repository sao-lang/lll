"""R7 Port 扩展协议（规格 P8）：MemoryPortWithRecall 承载语义召回。

recall 从 R2 基础 Port（hooks.MemoryPort）归位到 R7 能力层扩展协议：
基础 Port 只声明 read/write/forget 的通用记忆能力；语义召回（按查询返回相关项）
属 R7 领域语义，不该反向污染 R2。DTO（MemoryQuery/MemoryItem）由 R7 自有类型承载。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from compose import MemoryPort, PortCtx

from harness.memory.types import MemoryItem, MemoryQuery

__all__ = ["MemoryPortWithRecall"]


@runtime_checkable
class MemoryPortWithRecall(MemoryPort, Protocol):
    """R7 扩展：实现 MemoryPort 且支持语义召回的后端。"""

    async def recall(self, query: MemoryQuery, ctx: PortCtx) -> list[MemoryItem]:
        """语义召回：按查询返回相关记忆项。"""
        ...
