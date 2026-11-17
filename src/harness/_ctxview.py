"""R3/R4 共享动态上下文边界（编码规则 8）：把 HookCtx 的动态字段读取收敛到一处。

为何动态：ctx.meta 是规则 3 允许的「元数据透传」命名空间（键由装配层与各插件约定，
编译期不可枚举）；ctx.effect.payload 的 x-* 扩展键形态由外部 port 决定。
本模块是这些动态读取的**唯一**边界：coerce_ctx() 一次读取并归一化为具名视图，
下游插件只做属性访问 + 枚举比较，不再散落 meta.get / getattr。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from hooks import Decision, HookCtx, PortCtx
from runtime import DynamicValue, Result

__all__ = [
    "CtxView", "PortCtxView", "Dimension", "NextFn",
    "coerce_ctx", "coerce_port_ctx",
]

Dimension = tuple[str, str]
"""熔断/限流隔离维度：("provider", "openai") 这类二元组。"""

type NextFn = Callable[[], Awaitable[Decision]]
"""around 洋葱的下一层回调（由 HookBus 临时注入 ctx.meta['next']）。"""


@dataclass(frozen=True, slots=True)
class CtxView:
    """HookCtx 动态字段的具名视图（全部字段带类型，供下游纯属性访问）。

    字段：
        breaker_dimension: 熔断隔离维度，装配层写入；None 表示空维度。
        last_result: invoke_error 事件携带的失败结果（供熔断记录）。
        force_degrade: 是否强制降级（供 Degradation 插件）。
        bulkhead_pool: 舱壁池名，缺省 "default"。
        next_fn: around 下一层回调，仅 AROUND hook 执行期存在。
        agent_id / user_id: 成本归因维度（装配层写入）。
        idempotency_key: 副作用幂等键，来自 effect.payload 的 x-* 扩展键。
    """

    breaker_dimension: Dimension | None
    last_result: Result | None
    force_degrade: bool
    bulkhead_pool: str
    watchdog_kind: str | None
    next_fn: NextFn | None
    agent_id: str
    user_id: str
    idempotency_key: str | None


def coerce_ctx(ctx: HookCtx) -> CtxView:
    """唯一的动态读取边界：把 ctx.meta / effect.payload 读取一次归一化。

    参数：
        ctx: 钩子上下文（其 meta/payload 是动态命名空间）。
    返回：
        CtxView 具名视图；非法形态显式回退到安全默认值（规则 7）。
    """
    raw_dim: DynamicValue = ctx.meta.get("breaker_dimension")
    dimension: Dimension | None = None
    if isinstance(raw_dim, tuple) and len(raw_dim) == 2:
        dimension = (str(raw_dim[0]), str(raw_dim[1]))

    raw_result: DynamicValue = ctx.meta.get("last_result")
    last_result = raw_result if isinstance(raw_result, Result) else None

    raw_next: DynamicValue = ctx.meta.get("next")
    # 动态边界：callable 收窄后 cast 到具名回调类型（规则 4：getattr/callable 后 cast）。
    next_fn: NextFn | None = cast(NextFn, raw_next) if callable(raw_next) else None

    raw_pool: DynamicValue = ctx.meta.get("bulkhead_pool")
    pool = str(raw_pool) if raw_pool else "default"

    raw_kind: DynamicValue = ctx.meta.get("watchdog_kind")
    kind = str(raw_kind) if raw_kind else None

    raw_key: DynamicValue = ctx.effect.payload.get("x-idempotency-key")
    idem_key = str(raw_key) if raw_key else None

    return CtxView(
        breaker_dimension=dimension,
        last_result=last_result,
        force_degrade=bool(ctx.meta.get("force_degrade")),
        bulkhead_pool=pool,
        watchdog_kind=kind,
        next_fn=next_fn,
        agent_id=str(ctx.meta.get("agent_id") or ""),
        user_id=str(ctx.meta.get("user_id") or ""),
        idempotency_key=idem_key,
    )


@dataclass(frozen=True, slots=True)
class PortCtxView:
    """PortCtx.meta 动态字段的具名视图（成本归因维度）。

    字段：
        agent_id / user_id: 成本归因维度（装配层写入 meta）。
    """

    agent_id: str
    user_id: str


def coerce_port_ctx(ctx: PortCtx) -> PortCtxView:
    """PortCtx.meta 的唯一动态读取边界（规则 8），归名为成本归因维度。"""
    raw_agent: DynamicValue = ctx.meta.get("agent_id")
    raw_user: DynamicValue = ctx.meta.get("user_id")
    return PortCtxView(
        agent_id=str(raw_agent) if raw_agent else "",
        user_id=str(raw_user) if raw_user else "",
    )
