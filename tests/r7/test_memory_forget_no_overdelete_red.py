"""RED：forget(k2) 不得误删由 k1 写入、因内容去重合并进 k1 的记忆项。

裂缝：forget 除按 source 前缀匹配外，还用 `item.content == normalized` 宽松兜底。
两次 write 相同正文时 ingest 近重复合并只留一个 item（source=k1），forget(k2)
靠正文兜底删掉 k1 的 item → k1 的 KV 仍在但召回为空（静默数据丢失）。
修后：只按 source 前缀精确匹配；内容相同不是同一记忆，不得跨 key 误删。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import PortCtx
from harness.memory.memory import MemoryStore
from harness.memory.types import MemoryQuery

CTX = PortCtx(session_id="s", run_id="r", meta={"scope": {"tenant": "t"}})


def test_red_forget_one_key_keeps_identical_content_from_other() -> None:
    async def run() -> None:
        mem = MemoryStore()
        await mem.write("ns", "k1", "IDENTICAL_CONTENT_42", CTX)
        await mem.write("ns", "k2", "IDENTICAL_CONTENT_42", CTX)

        await mem.forget("ns", "k2", CTX)

        assert await mem.read("ns", "k1", CTX) == "IDENTICAL_CONTENT_42"
        recalled = await mem.recall(
            MemoryQuery(text="IDENTICAL_CONTENT_42", top_k=5), CTX)
        assert [i.content for i in recalled] == ["IDENTICAL_CONTENT_42"], \
            "forget(k2) 不得让 k1 的相同正文记忆从召回消失"

    asyncio.run(run())


def test_red_forget_own_key_still_removes_recall() -> None:
    async def run() -> None:
        mem = MemoryStore()
        await mem.write("ns", "k1", "OWN_CONTENT", CTX)
        await mem.forget("ns", "k1", CTX)
        assert await mem.read("ns", "k1", CTX) is None
        recalled = await mem.recall(MemoryQuery(text="OWN_CONTENT", top_k=5), CTX)
        assert recalled == [], "forget 自己的 key 仍须从召回移除"

    asyncio.run(run())
