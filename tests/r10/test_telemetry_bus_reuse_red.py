"""RED：新 LifecycleBus 不得因 id 复用被误判为"已注册"而漏订阅。

裂缝：register 用 id(bus) 作已注册集合；bus 每次 say 新建、GC 后 id 被新 bus
复用，新 bus 因 id 命中旧记录被跳过，session_start emit 不产任何 span（对应
审计中"5 次 say 只 4 个 turn + 孤儿 span"）。修后：按 bus 对象身份判重，
id 复用不再影响订阅。
"""
from __future__ import annotations

import gc

from compose import LifecycleBus, LifecyclePhase, Notice
from harness.observability import Trace


def test_each_new_bus_is_subscribed_after_gc_id_reuse() -> None:
    """连续创建/释放 bus（强制 GC 复用 id），每个新 bus 都必须真订阅。"""
    trace = Trace(trace_id="bus-reuse")
    for i in range(60):
        bus = LifecycleBus()
        trace.register(bus)
        before = len(trace.spans())
        bus.emit(Notice(phase=LifecyclePhase.SESSION_START, session_id=f"s{i}"))
        after = len(trace.spans())
        assert after > before, (
            f"第 {i} 个 bus 的 session_start 未产出 span（id 复用导致漏注册）；"
            f"id(bus)={id(bus)} registered={trace._registered_buses}"
        )
        del bus
        gc.collect()  # 释放后 id 可被下一个新 bus 复用（旧实现会命中陈旧记录）
