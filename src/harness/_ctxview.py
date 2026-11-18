"""R4 PortCtx 动态上下文边界（编码规则 6）：把 PortCtx 的动态字段读取收敛到一处。

ARCH-v5：HookCtx 侧的动态视图已删除（插件改用 RequestScope 具名字段）。
本模块只保留 gateway 需要的 PortCtx 投影与成本归因维度具名读取。
"""
from __future__ import annotations

from dataclasses import dataclass

from hooks import PortCtx
from runtime import DynamicValue

__all__ = [
    "PortCtxView",
    "as_port_ctx",
    "coerce_port_ctx",
]


@dataclass(frozen=True, slots=True)
class PortCtxView:
    """PortCtx.meta 动态字段的具名视图（成本归因维度）。"""

    agent_id: str
    user_id: str


def coerce_port_ctx(ctx: PortCtx) -> PortCtxView:
    """PortCtx.meta 的唯一动态读取边界（规则 6），归名为成本归因维度。"""
    raw_agent: DynamicValue = ctx.meta.get("agent_id")
    raw_user: DynamicValue = ctx.meta.get("user_id")
    return PortCtxView(
        agent_id=str(raw_agent) if raw_agent else "",
        user_id=str(raw_user) if raw_user else "",
    )


def as_port_ctx(ctx: object) -> PortCtx:
    """把 Runner 上下文投影为 PortCtx（规则 6 唯一动态读取边界）。

    session_id/run_id/meta 都在此一次性读取归名，gateway 侧只传结果。
    """
    meta: DynamicValue = getattr(ctx, "meta", None)
    session_id = str(getattr(ctx, "session_id", "") or "")
    return PortCtx(
        session_id=session_id,
        # RuntimeCtx 无独立 run_id；本架构 run 与 session 同生命周期，回退 session_id
        # 让成本按 run 归因可用（E7 修复，原来恒空字符串）。
        run_id=str(getattr(ctx, "run_id", "") or "") or session_id,
        meta=dict(meta) if isinstance(meta, dict) else {},
    )
