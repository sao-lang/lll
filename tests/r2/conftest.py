"""R2 测试夹具：可复用 Hook 与 HookBus。"""
from __future__ import annotations

from hooks import Decision, HookBus, HookCtx, HookMode


class BaseHook:
    """测试用 Hook 基类，提供默认属性。"""
    name = "base"
    events = ()
    mode = HookMode.GUARD
    priority = 100
    requires_ports = ()

    async def __call__(self, ctx: HookCtx) -> Decision:
        return Decision.continue_()


def make_ctx(effect, state, event="", session_id="s1") -> HookCtx:
    """构造 HookCtx 便捷函数。"""
    return HookCtx(effect=effect, state=state, session_id=session_id, event=event)
