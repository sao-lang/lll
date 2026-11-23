"""R10 测试夹具：本地 fake 事件源与时钟，无联网无真实 LLM。"""
from __future__ import annotations

from compose import PortCtx


def pctx(session_id: str = "s10", run_id: str = "run10") -> PortCtx:
    """构造 R10 用的 PortCtx。"""
    return PortCtx(session_id=session_id, run_id=run_id)
