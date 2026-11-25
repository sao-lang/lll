"""RED：Bulkhead 满池 DEGRADE 路径不得释放别人的槽位。

裂缝：run_in_slot 在 acquire 失败（池满）时，DEGRADE 写 fault 后仍进入
try/finally，finally 无条件 release → 释放了持槽者的槽，舱壁容量被击穿。
修后：只有真正 acquire 成功的调用才在 finally 释放。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from runtime import Invoke, Result
from compose.context import CallContext
from harness.resilience.defenses import Bulkhead, _ExceedPolicy


def _inv() -> CallContext:
    return CallContext(effect=Invoke(port="llm"), ctx=object(),
                       session=type("S", (), {"session_id": "s"})())


def test_red_degraded_call_does_not_release_holder_slot() -> None:
    async def run() -> None:
        b = Bulkhead(pools={"default": 1}, on_full=_ExceedPolicy.DEGRADE.value,
                     pool="default")
        inv = _inv()
        gate = asyncio.Event()

        async def slow(i):
            await gate.wait()
            return Result.success("slow")

        holder = asyncio.create_task(b.run_in_slot(inv, slow))
        await asyncio.sleep(0)
        assert b._used["default"] == 1

        # 池满：DEGRADE 调用仍执行，但不得释放持槽者的槽
        degraded = await b.run_in_slot(inv, lambda i: _ok())
        assert degraded.ok
        assert b._used["default"] == 1, \
            f"DEGRADE 调用没占槽，不得释放别人的槽，实际 used={b._used['default']}"

        gate.set()
        await holder
        assert b._used["default"] == 0

    async def _ok() -> Result:
        return Result.success("ok")

    asyncio.run(run())


def test_red_reject_path_does_not_release_holder_slot() -> None:
    async def run() -> None:
        b = Bulkhead(pools={"default": 1}, on_full=_ExceedPolicy.REJECT.value,
                     pool="default")
        inv = _inv()
        gate = asyncio.Event()

        async def slow(i):
            await gate.wait()
            return Result.success("slow")

        holder = asyncio.create_task(b.run_in_slot(inv, slow))
        await asyncio.sleep(0)
        rejected = await b.run_in_slot(inv, lambda i: _ok())
        assert not rejected.ok and rejected.error and rejected.error.code == "BULKHEAD_FULL"
        assert b._used["default"] == 1, \
            f"REJECT 路径也不得释放别人的槽，实际 used={b._used['default']}"
        gate.set()
        await holder

    asyncio.run(run())
