"""R2 测试夹具：make_ctx 构造 HookCtx（供 v5 编排测试桥接 R1 用）。"""
from __future__ import annotations

from hooks import HookCtx


def make_ctx(effect, state, event="", session_id="s1") -> HookCtx:
    """构造 HookCtx 便捷函数。"""
    return HookCtx(effect=effect, state=state, session_id=session_id, event=event)
