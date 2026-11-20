"""R7 大结果卸载测试（FR-5）：BlobPort 句柄可读、删除、Offloader 协议。"""
from __future__ import annotations

import asyncio

from harness.context.offloader import (
    BLOB_HANDLE_SCHEME,
    DefaultOffloader,
    InMemoryBlobStore,
    make_handle,
    parse_handle,
)


def test_blob_put_get_roundtrip() -> None:
    """put 返句柄，get 按句柄读回原值。"""
    async def run() -> None:
        store = InMemoryBlobStore()
        handle = await store.put("大结果正文")
        assert handle.startswith(BLOB_HANDLE_SCHEME)
        assert await store.get(handle) == "大结果正文"

    asyncio.run(run())


def test_blob_delete_then_none() -> None:
    """delete 后句柄不可读，get 返 None。"""
    async def run() -> None:
        store = InMemoryBlobStore()
        handle = await store.put({"data": [1, 2, 3]})
        assert await store.get(handle) == {"data": [1, 2, 3]}
        await store.delete(handle)
        assert await store.get(handle) is None

    asyncio.run(run())


def test_handle_parse() -> None:
    """句柄格式 store://id，可解析出 id。"""
    handle = make_handle("abc123")
    assert handle == "store://abc123"
    assert parse_handle(handle) == "abc123"
    assert parse_handle("bad://x") is None


def test_get_unknown_handle_none() -> None:
    """未知句柄 get 返 None（fail-closed，不抛错）。"""
    async def run() -> None:
        store = InMemoryBlobStore()
        assert await store.get("store://nope") is None

    asyncio.run(run())


def test_offloader_offload_returns_readable_handle() -> None:
    """Offloader 经 BlobPort 卸载，返回句柄可读回。"""
    async def run() -> None:
        store = InMemoryBlobStore()
        offloader = DefaultOffloader()
        handle = await offloader.offload("big payload", store)
        assert await store.get(handle) == "big payload"
        assert offloader.name == "default_offloader"

    asyncio.run(run())
