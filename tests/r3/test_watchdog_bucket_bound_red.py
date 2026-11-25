"""RED：Watchdog 会话历史桶必须有上限，防止多租户长期运行内存泄漏。

裂缝：会话隔离修复把单 list 换成 dict[str, list]，但无淘汰机制——2000 个
不同 session 后 len(_history)=2000 且持续增长。生产多租户长生命周期策略实例
会无界泄漏。修后：桶数受 max_sessions 上限约束（LRU 淘汰最久未活动桶），
且活跃会话的检测语义不变。
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import PortCtx, ToolCall
from runtime import Invoke
from compose.context import CallContext
from harness.resilience.defenses import Watchdog


def _inv(sid: str, port: str = "llm") -> CallContext:
    return CallContext(effect=Invoke(port=port), ctx=object(),
                       session=type("S", (), {"session_id": sid})())


def test_red_history_buckets_bounded() -> None:
    wd = Watchdog(max_sessions=8)
    for i in range(200):
        wd.detect(_inv(f"session-{i}"))
    assert len(wd._history) <= 8, \
        f"历史桶应有上限 8，实际 {len(wd._history)}（内存泄漏）"


def test_red_active_session_still_detected_after_eviction() -> None:
    wd = Watchdog(max_sessions=4, repeat_k=3, detect_abab=True)
    # 活跃会话 S 连续活动，期间混入大量其它会话触发淘汰
    for i in range(20):
        wd.detect(_inv(f"other-{i}"))
        wd.detect(_inv("S"))
    # S 的近期历史仍完整 → 3 连命中
    assert wd.detect(_inv("S")) is True, \
        "活跃会话最近 3 次 llm 应命中循环，淘汰不得清空活跃桶"
