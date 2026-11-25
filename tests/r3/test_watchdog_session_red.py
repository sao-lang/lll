"""RED：Watchdog 循环检测必须按会话隔离，跨会话不串扰。

裂缝：_history 是实例级全局 list，会话 A(llm)/B(rag)/A(llm)/B(rag) 交替时，
B 的第 2 次调用因全局历史形成 abab 被误拒（INVOKE_REJECTED）。
修后：历史按 session_id 分桶，inspect 兼容无 session 的直接调用。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from runtime import Invoke, Result
from compose.context import CallContext
from harness.resilience.defenses import Watchdog
from harness.resilience.executor import ReliabilityExecutor


def _inv(sid: str, port: str) -> CallContext:
    return CallContext(effect=Invoke(port=port), ctx=object(),
                       session=type("S", (), {"session_id": sid})())


def test_red_watchdog_isolates_sessions() -> None:
    async def core(i): return Result.success("ok")
    wd = Watchdog(window=10, repeat_k=99, detect_abab=True)
    ex = ReliabilityExecutor(core, watchdog=wd)
    outcomes = []
    for sid, port in [("A", "llm"), ("B", "rag"), ("A", "llm"), ("B", "rag")]:
        r = asyncio.run(ex.run_invocation(_inv(sid, port)))
        outcomes.append(r.ok)
    assert outcomes == [True, True, True, True], \
        f"跨会话交替不应触发循环误拒，实际 {outcomes}"


def test_red_inspect_direct_call_unchanged() -> None:
    abab = Watchdog(window=10, repeat_k=0, detect_abab=True)
    assert [abab.inspect(k) for k in ("a", "b", "a", "b")] == [False, False, False, True]
