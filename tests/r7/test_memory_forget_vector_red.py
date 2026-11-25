"""RED：forget 必须同步清除共享 VectorStore 里的 mem: 块，否则 RAG 仍可召回已遗忘内容。

裂缝：MemoryStore.ingest 把 mem:{item_id} 块 upsert 进注入的 vector_store；forget
只清 _items/_vectors，标注"召回链只读 _items 所以不可召回"。但同一 store 注入
RAGPipeline 后残留块可被检索，形成"已遗忘仍可被其他消费端召回"的隔离漏洞。
修后：VectorStore 协议提供 delete(chunk_ids)，forget 删除对应 mem: 块。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import PortCtx
from harness.memory.memory import MemoryStore
from harness.memory.types import MemoryQuery
from harness.rag.pipeline import RAGPipeline
from harness.rag.store import InMemoryVectorStore
from harness.rag.types import Document


def test_red_forget_removes_shared_vector_chunk() -> None:
    async def run() -> None:
        store = InMemoryVectorStore()
        ctx = PortCtx(session_id="s", run_id="r")
        mem = MemoryStore(vector_store=store)

        await mem.write("ns", "k", "QR7X_SPECIAL_TOKEN content", ctx)
        # 向量在 recall 的 _ensure_vectors 阶段 upsert 进共享 store（真实路径）
        await mem.recall(MemoryQuery(text="QR7X_SPECIAL_TOKEN", top_k=5), ctx)
        assert len(store._chunks) == 1, "recall 后应有共享向量块"

        await mem.forget("ns", "k", ctx)
        assert store._chunks == [], \
            f"forget 后共享向量库不得残留已遗忘块，实际 {[c.chunk_id for c in store._chunks]}"

    asyncio.run(run())
