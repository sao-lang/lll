"""RED：MemoryStore.write 写入的记忆必须可被 recall 召回（全模块体检 B7）。

现状 write 只写 self._kv，recall 只读 self._items——两条互不相通的通道。
按标准 MemoryPort.write 写入后 recall 命中 0（demo main.py:188-189 正是这么用）。
本测试证明这个断裂，当前应 FAIL。
"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.memory import MemoryQuery, MemoryStore


def test_red_written_memory_is_recallable() -> None:
    """write 后 recall 相近文本应命中（当前 FAIL：write 不进召回链）。"""
    async def run() -> None:
        ms = MemoryStore()
        ctx = PortCtx(session_id="s-red", run_id="r", meta={"tenant": "t1"})
        await ms.write("profile", "language", "用户偏好用中文交流", ctx)

        hits = await ms.recall(MemoryQuery(text="中文", top_k=3), ctx)
        assert hits, "write 写入的记忆必须可被 recall 召回（write 与 recall 同一通道）"
        assert any("中文" in h.content for h in hits), (
            f"应召回含中文的记忆，实得 {[h.content for h in hits]}"
        )

    asyncio.run(run())
