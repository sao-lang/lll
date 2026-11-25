"""RED：forget 必须让已 ingest 的记忆不可召回；KV 层必须按 tenant 隔离。

独立审计 EXP5-C/D 证据：
- write 自动 ingest 到 _items/_vectors/_tiers，forget 只 pop _kv → recall 仍命中。
- read 用 (namespace, key) 直查 _kv，跨 tenant 可读。
"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.memory.memory import MemoryStore
from harness.memory.types import MemoryQuery, MemoryScope


def _ctx(tenant: str) -> PortCtx:
    return PortCtx(session_id="s-forget", run_id="r-forget",
                   meta={"scope": MemoryScope(tenant=tenant)})


def test_red_forget_removes_ingested_memory_from_recall() -> None:
    async def run() -> None:
        ctx = _ctx("t1")
        store = MemoryStore()
        await store.write("ns", "k", "remember the blue bicycle", ctx)
        query = MemoryQuery(text="blue bicycle", top_k=5, scope=MemoryScope(tenant="t1"))

        before = await store.recall(query, ctx)
        assert [i.content for i in before] == ["remember the blue bicycle"]

        await store.forget("ns", "k", ctx)

        assert await store.read("ns", "k", ctx) is None
        after = await store.recall(query, ctx)
        assert after == [], f"forget 后不得召回，实际 {[i.content for i in after]}"
        assert store.tier_count() == 0, "forget 必须同步清理温度层登记"

    asyncio.run(run())


def test_red_kv_read_is_tenant_isolated() -> None:
    async def run() -> None:
        store = MemoryStore()
        await store.write("ns", "k", "t1-secret-value", _ctx("t1"))

        assert await store.read("ns", "k", _ctx("t1")) == "t1-secret-value"
        assert await store.read("ns", "k", _ctx("t2")) is None, "跨 tenant 不得读到 t1 的 KV"

    asyncio.run(run())


def test_red_kv_forget_does_not_erase_other_tenant() -> None:
    async def run() -> None:
        store = MemoryStore()
        await store.write("ns", "k", "t1-value", _ctx("t1"))
        await store.write("ns", "k", "t2-value", _ctx("t2"))

        await store.forget("ns", "k", _ctx("t1"))

        assert await store.read("ns", "k", _ctx("t1")) is None
        assert await store.read("ns", "k", _ctx("t2")) == "t2-value", "忘掉 t1 不得影响 t2"

    asyncio.run(run())
