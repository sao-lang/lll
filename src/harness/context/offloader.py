"""R7 大结果卸载：BlobPort 协议 + 内存默认实现 + Offloader。

句柄形如 ``store://<id>``，可按句柄读回（修复原设计误用 StorePort 的语义错位）。
BlobPort 是 context 包内部协议（仿 R6 CachePort），不进 PORT_PROTOCOLS。
"""
from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from runtime import DynamicValue

__all__ = [
    "BLOB_HANDLE_SCHEME",
    "BlobPort",
    "Offloader",
    "DefaultOffloader",
    "InMemoryBlobStore",
    "make_handle",
    "parse_handle",
]

# 句柄 scheme（具名常量，禁止裸魔法串）。
BLOB_HANDLE_SCHEME = "store://"


def make_handle(blob_id: str) -> str:
    """把 blob id 包装为句柄字符串。"""
    return f"{BLOB_HANDLE_SCHEME}{blob_id}"


def parse_handle(handle: str) -> str | None:
    """从句柄解析 blob id；scheme 不符返回 None。"""
    if not handle.startswith(BLOB_HANDLE_SCHEME):
        return None
    return handle[len(BLOB_HANDLE_SCHEME) :] or None


class BlobPort(Protocol):
    """大结果卸载存储协议：存单值→返句柄→按句柄读回/删除。"""

    name: str

    async def put(self, value: DynamicValue) -> str:
        """存入一个值，返回可读句柄。"""
        ...

    async def get(self, handle: str) -> DynamicValue | None:
        """按句柄读回；不存在返回 None。"""
        ...

    async def delete(self, handle: str) -> None:
        """按句柄删除；不存在幂等无操作。"""
        ...


class Offloader(Protocol):
    """卸载协议：把大值交给 BlobPort 存并返回句柄。"""

    name: str

    async def offload(self, value: DynamicValue, store: BlobPort) -> str:
        """卸载值，返回可读取的句柄。"""
        ...


class InMemoryBlobStore:
    """内存 Blob 存储：进程内字典，句柄 uuid 唯一。"""

    name: str = "in_memory_blob"

    def __init__(self) -> None:
        """初始化空存储。"""
        self._data: dict[str, DynamicValue] = {}

    async def put(self, value: DynamicValue) -> str:
        """存值并返回 ``store://<uuid>`` 句柄。"""
        blob_id = uuid4().hex
        self._data[blob_id] = value
        return make_handle(blob_id)

    async def get(self, handle: str) -> DynamicValue | None:
        """按句柄读回；句柄格式错或不存在返 None。"""
        blob_id = parse_handle(handle)
        if blob_id is None:
            return None
        return self._data.get(blob_id)

    async def delete(self, handle: str) -> None:
        """按句柄删除；不存在幂等。"""
        blob_id = parse_handle(handle)
        if blob_id is not None:
            self._data.pop(blob_id, None)


class DefaultOffloader:
    """默认卸载器：委托任意 BlobPort 存值并回传句柄。"""

    name: str = "default_offloader"

    async def offload(self, value: DynamicValue, store: BlobPort) -> str:
        """把值交给 store 存并返回句柄。"""
        return await store.put(value)
