"""R7 StorePort 默认实现：内存会话快照存取（B.1.2 勾销项）。

签名对齐 R2 ``StorePort``（async save/load）；快照须可确定性 JSON 序列化，
内部做深拷贝隔离，避免调用方与存储共享可变状态。
"""
from __future__ import annotations

from copy import deepcopy

from runtime import DynamicValue

__all__ = ["InMemoryStorePort"]


class InMemoryStorePort:
    """内存会话快照存储：进程内字典，session_id 为键。"""

    name: str = "in_memory_store_port"

    def __init__(self) -> None:
        """初始化空快照表。"""
        self._snapshots: dict[str, dict[str, DynamicValue]] = {}

    async def save(self, session_id: str, snapshot: dict[str, DynamicValue]) -> None:
        """保存会话快照（深拷贝隔离）。"""
        self._snapshots[session_id] = deepcopy(snapshot)

    async def load(self, session_id: str) -> dict[str, DynamicValue] | None:
        """读取快照副本；不存在返回 None。"""
        snap = self._snapshots.get(session_id)
        return deepcopy(snap) if snap is not None else None
