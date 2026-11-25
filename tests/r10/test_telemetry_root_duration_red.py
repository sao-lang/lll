"""RED：session root span 必须在 session_end 收口并记录真实耗时。

裂缝：_on_phase 的 session_end 分支只 _close_turn()，从不收 root，root 的
duration_ms 恒为 0（Jaeger 上根 span 永远显示 0ms）。修后：会话终态写 root
span 的真实 duration，且约等于 session_start→session_end 的实际经过时间。
"""
from __future__ import annotations

import time

from compose import LifecycleBus, LifecyclePhase, Notice
from harness.observability import Trace


def test_root_span_has_real_duration_after_session_end() -> None:
    """root span 在 session_end 后 duration>0 且≈真实经过时间（不是恒 0）。"""
    trace = Trace(trace_id="root-dur")
    bus = LifecycleBus()
    bus.subscribe(trace)

    t0 = time.perf_counter()
    bus.emit(Notice(phase=LifecyclePhase.SESSION_START, session_id="s"))
    time.sleep(0.05)  # 真实耗时，保证 duration 必须非零且可度量
    bus.emit(Notice(phase=LifecyclePhase.SESSION_END, session_id="s"))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    roots = [s for s in trace.spans() if s.name == "session"]
    assert len(roots) == 1, f"应有唯一 root，实际 {[s.name for s in trace.spans()]}"
    root = roots[0]
    assert root.duration_ms > 0.0, (
        f"root 必须在 session_end 记真实 duration，实际 duration_ms={root.duration_ms}"
    )
    assert abs(root.duration_ms - elapsed_ms) <= 10.0, (
        f"root duration 必须≈真实耗时 {elapsed_ms:.3f}ms，实际 {root.duration_ms:.3f}ms"
    )
