"""R9 测试夹具：本地 fake 三 Port + 确定性安全上下文，无联网无真实 IO。"""
from __future__ import annotations

from compose import PortCtx


def pctx(session_id: str = "s9", run_id: str = "run9") -> PortCtx:
    """构造 R9 用的 PortCtx。"""
    return PortCtx(session_id=session_id, run_id=run_id)
