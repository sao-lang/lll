"""R3 拆分（规格 P3）：②降级守卫 + 具名故障/typed 目标 DTO，承接原 AOP 降级状态机。

四坑语义（B1/B2/R3-6/R3-7）在 Degradation 实例上原样保留，本模块只换宿主：
从 Invocation.meta 改为组合层 CallState(ctx.meta)。DTO 不依赖 src/aop/（无兼容层）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from runtime import Effect, Result

from .call import Guard, Verdict

__all__ = [
    "FaultRecord", "RoutingTarget", "CallState", "DegradationGuard",
    "degrade_retry",
]


class _Keys:
    """ctx.meta 具名协作键（替代旧 Invocation.meta 裸键），集中定义禁用魔法字符串。"""
    FAULT: Final[str] = "fault"
    ROUTING_TARGET: Final[str] = "routing_target"


@dataclass(frozen=True, slots=True)
class FaultRecord:
    """具名故障记录（四坑语义载体）。

    Attributes:
        reason: 故障原因（rate_limited/retry_exhausted/timeout/breaker_open...），只进 trace。
        failed_target: 降级阶梯后端注册名（B2：非熔断维度二元组；R3-7 retry/timeout 必带）。
    """
    reason: str
    failed_target: str = ""


@dataclass(frozen=True, slots=True)
class RoutingTarget:
    """typed 降级目标（②守卫裁决 → 网关消费，与 Registry 同源）。

    Attributes:
        target: 后端注册名或 "refuse"。
        reason: 降级原因（只进 trace，不参与选后端）。
    """
    target: str
    reason: str = ""


class CallState:
    """call 层具名协作状态：从 ctx.meta 读写 fault / routing_target（替代 Invocation.meta）。

    状态存在调用上下文（R1 RuntimeCtx.meta），守卫实例另持跨调用策略状态；
    单次流程进度不进守卫实例（D-8）。
    """
    def __init__(self, ctx: object) -> None:
        """绑定调用上下文。

        Args:
            ctx: R1 RuntimeCtx（含 meta 字典）或测试最小 ctx。
        """
        self._ctx = ctx

    def _meta(self) -> dict[str, object]:
        """取 ctx.meta 字典（缺省构造空字典挂回 ctx，保证读写同源）。"""
        meta = getattr(self._ctx, "meta", None)
        if not isinstance(meta, dict):
            meta = {}
            try:
                setattr(self._ctx, "meta", meta)
            except (AttributeError, TypeError):  # ctx 不可写（如 frozen）：仅本地读写
                pass
        return meta

    def write_fault(self, fault: FaultRecord) -> None:
        """写入具名故障记录（③装饰器/②守卫报告故障的唯一入口）。"""
        self._meta()[_Keys.FAULT] = fault

    def fault(self) -> FaultRecord | None:
        """读取当前故障记录；无或类型不符返回 None。"""
        value = self._meta().get(_Keys.FAULT)
        return value if isinstance(value, FaultRecord) else None

    def write_routing_target(self, target: RoutingTarget) -> None:
        """写入 typed 降级目标（②守卫裁决后、网关消费前）。"""
        self._meta()[_Keys.ROUTING_TARGET] = target

    def routing_target(self) -> RoutingTarget | None:
        """读取 typed 降级目标；无或类型不符返回 None。"""
        value = self._meta().get(_Keys.ROUTING_TARGET)
        return value if isinstance(value, RoutingTarget) else None

    def session_id(self) -> str:
        """从 ctx 派生会话标识（B1 选级隔离维度的单一入口）。"""
        return str(getattr(self._ctx, "session_id", "") or "")


class DegradationGuard:
    """②降级守卫：包装现有 Degradation.select（四坑逻辑原样），实现 Guard 协议。

    check：放行（降级裁决在 observe 后置做，因需先看到 Result 失败）；
    observe：读故障记录 → select 选级 → 写 typed target（四坑语义不变）。
    """
    name = "degrade_guard"

    def __init__(self, degradation: object) -> None:
        """绑定降级裁决实例（持 ladder + available + 按 session 隔离状态）。

        Args:
            degradation: harness.resilience.Degradation 实例（duck-typed：需有 select）。
        """
        self._degradation = degradation

    async def check(self, effect: Effect, ctx: object) -> Verdict | None:
        """准入：降级守卫不拦截，返回 None 放行（裁决在 observe 后置）。"""
        return None

    async def observe(self, effect: Effect, result: Result, ctx: object) -> None:
        """后置裁决：读故障记录 → Degradation.select（四坑）→ 写 typed target。"""
        state = CallState(ctx)
        fault = state.fault()
        if fault is None:
            return
        select = getattr(self._degradation, "select")
        target = select(fault, session_id=state.session_id())
        state.write_routing_target(RoutingTarget(target=str(target), reason=fault.reason))


# CallFn from .call for decorator signatures
from .call import CallFn  # noqa: E402 - 避免顶部循环，契约清晰


def degrade_retry(
    base: CallFn,
    *,
    guards: tuple[DegradationGuard, ...] = (),
) -> CallFn:
    """③call 装饰器驱动的链外重跑：失败 → 降级守裁决 → 按 typed target 重跑。

    四坑语义由 DegradationGuard.observe 内的 select 承担；本装饰器只驱动
    "失败后重跑"的循环（替代旧 AOP Salvage + 容器重路由状态机）。

    Args:
        base: 中心 dispatch（按 effect.port 分派）。
        guards: 降级守卫序列（通常一个）。

    Returns:
        新 CallFn（失败且守卫产出非 refuse 目标时重跑）。
    """
    async def call(effect: Effect, ctx: object) -> Result:
        result = await base(effect, ctx)
        if result.ok:
            return result
        for guard in guards:
            await guard.observe(effect, result, ctx)
        target = CallState(ctx).routing_target()
        if target is None or target.target == "refuse":
            return result
        # 链外重跑：切到 typed target 后重调 base（目标经 ctx.meta 传递，由 dispatch 消费）
        return await base(effect, ctx)
    return call
