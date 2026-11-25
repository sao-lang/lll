"""RED：VectorStore.upsert 必须按 chunk_id 覆盖去重，不追加重复块。

独立审计 EXP5-B 证据：同一 doc ingest 两次 → store 内 2 个同 chunk_id 块。
"""
from __future__ import annotations

import asyncio

from harness.rag.store import InMemoryVectorStore
from harness.rag.types import Chunk


def test_red_upsert_same_chunk_id_overwrites_instead_of_appending() -> None:
    async def run() -> None:
        store = InMemoryVectorStore()
        await store.upsert([Chunk(chunk_id="d1:0", doc_id="d1", text="old", vector=(1.0, 0.0))])
        await store.upsert([Chunk(chunk_id="d1:0", doc_id="d1", text="new", vector=(0.0, 1.0))])

        chunks = store.all_chunks()
        assert len(chunks) == 1, f"同 chunk_id 只能有 1 块，实际 {len(chunks)}"
        assert chunks[0].text == "new", f"后写应覆盖先写，实际 {chunks[0].text!r}"
        assert chunks[0].vector == (0.0, 1.0)

    asyncio.run(run())


def test_red_upsert_batch_dedups_and_preserves_distinct_ids() -> None:
    async def run() -> None:
        store = InMemoryVectorStore()
        await store.upsert([
            Chunk(chunk_id="a", doc_id="d", text="a1", vector=(1.0, 0.0)),
            Chunk(chunk_id="b", doc_id="d", text="b1", vector=(1.0, 0.0)),
        ])
        await store.upsert([
            Chunk(chunk_id="a", doc_id="d", text="a2", vector=(1.0, 0.0)),
            Chunk(chunk_id="c", doc_id="d", text="c1", vector=(1.0, 0.0)),
        ])

        by_id = {c.chunk_id: c.text for c in store.all_chunks()}
        assert len(store.all_chunks()) == 3, f"应恰好 3 块，实际 {len(store.all_chunks())}"
        assert by_id == {"a": "a2", "b": "b1", "c": "c1"}

    asyncio.run(run())
