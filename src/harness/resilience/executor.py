"""R3 可靠性执行器（去角色化）：按可靠性工程语义显式编排领域策略。

本模块**没有**任何横切角色概念——没有角色协议、没有角色枚举、没有槽位列表，
也没有统一的方法名契约。每个领域策略
以**自身的领域方法**参与（KillSwitch.is_tripped / RateLimit.acquire /
CircuitBreaker.permit / Degradation.select ...），执行器用显式代码按可靠性
工程语义串联：

    前置放行 → 预算内中心调用（超时/重试/舱位）→ 熔断记数 → 失败降级选级 + 重路由

这些是可靠性领域固有语义，不是 AOP 的五层横切角色。装配由
``ResilienceCapability.install()`` 用 opt-in 构造注入完成，产出可信注入 R1 的
CallFn；缺席的策略即跳过，无需占位实现。

四坑（B1 session 隔离 / B2 failed_target 注册名 / R3-6 永久剔除 / R3-7 fault
带 failed_target）由 Degradation.select + FaultRecord 保住，断言不放水。
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping

from compose.context import CallContext, FaultRecord, Invocation, RoutingTarget
from runtime import CallFn, Effect, Result

from .breaker import CircuitBreaker
from .defenses import (
    Bulkhead,
    Degradation,
    Idempotency,
    KillSwitch,
    RateLimit,
    RatePermit,
    Watchdog,
    failed_target_of,
)
from .retry import RetryPolicy
from .timeout import Timeout

__all__ = ["ReliabilityExecutor"]

_MAX_REROUTE = 8
"""降级重路由硬上限（防阶梯成环或异常配置导致死循环）。"""
_REFUSE_TARGET = "refuse"
"""末级拒答目标名（与 DegradeMode.REFUSE 对齐）。"""

type _InvCall = Callable[[Invocation], Awaitable[Result]]
"""领域调用签名（Invocation → Result）。"""
type _Transformer = Callable[[_InvCall], _InvCall]
"""函数式领域调用变换器（extra_plugins 扩展形态，非角色插件）。"""


async def _await_result(outcome: Result | Awaitable[Result]) -> Result:
    """统一等待 core 的同步/异步返回（具名边界，避免 Any 泄漏）。"""
    return await outcome if inspect.isawaitable(outcome) else outcome


def _as_invocation(effect: Effect, ctx: object) -> Invocation:
    """由 (effect, ctx) 构造 Invocation；ctx 已是 Invocation 则复用（共享 meta）。

    meta 与 R1 RuntimeCtx.meta 同源：前置策略写出的 fault / typed target 与
    网关从 ctx.meta 读取的是同一份（B3 状态统一，重路由不会静默失效）。
    """
    if isinstance(ctx, Invocation):
        return ctx
    # 规则 6 例外：meta/session/session_id 均为 R1 RuntimeCtx 具名固定字段，非动态键
    ctx_meta = getattr(ctx, "meta", None)
    shared: dict[str, object] = ctx_meta if isinstance(ctx_meta, dict) else {}
    session = getattr(ctx, "session", None)
    if session is None:
        from types import SimpleNamespace
        # 规则 6 例外：session_id 是 R1 RuntimeCtx 具名固定字段
        session = SimpleNamespace(session_id=str(getattr(ctx, "session_id", "") or ""))
    return CallContext(effect=effect, ctx=ctx, session=session, meta=shared)


def _idem_key(inv: Invocation) -> str | None:
    """从调用派生幂等键（Invoke.idempotency_key），非 Invoke 返回 None。"""
    effect = inv.effect
    # 规则 6 例外：idempotency_key 是 R1 Invoke 具名固定字段
    return str(getattr(effect, "idempotency_key", "") or "") or None


class ReliabilityExecutor:
    """可靠性执行器：持具名领域策略，显式编排一次可靠调用。

    构造注入全部 opt-in——传实例才参与，None 即该领域能力缺席（不写空实现）。
    """

    def __init__(
        self,
        core: Callable[[Invocation], Result | Awaitable[Result]],
        *,
        registry: object | None = None,
        bus: object | None = None,
        notify: Callable[[str, Mapping[str, object]], None] | None = None,
        kill_switch: KillSwitch | None = None,
        watchdog: Watchdog | None = None,
        idempotency: Idempotency | None = None,
        rate_limit: RateLimit | None = None,
        bulkhead: Bulkhead | None = None,
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
        timeout: Timeout | None = None,
        degradation: Degradation | None = None,
        transformers: tuple[_Transformer, ...] = (),
    ) -> None:
        """装配执行器：core 是中心 Port 调用（如 ModelGateway.as_call）。"""
        self._core = core
        self.registry = registry
        self._bus = bus
        self._notify = notify or (lambda _phase, _payload: None)
        self._kill_switch = kill_switch
        self._watchdog = watchdog
        self._idempotency = idempotency
        self._rate_limit = rate_limit
        self._bulkhead = bulkhead
        self._breaker = breaker
        self._retry = retry
        self._timeout = timeout
        self._degradation = degradation
        self._transformers = tuple(transformers)

    # ---- 通知（16 相位之一：invoke_rejected）----
    def _emit_rejected(self, inv: Invocation, reason: str) -> None:
        """发 invoke_rejected（每次调用只发一次，meta 标记去重）。"""
        if inv.meta.get("_rejected_emitted"):
            return
        inv.meta["_rejected_emitted"] = True
        if self._bus is not None:
            from compose import LifecyclePhase, Notice  # 延迟导入避免循环
            # 规则 6 例外：emit 是 LifecycleBus 具名固定能力探测，非动态键
            emit = getattr(self._bus, "emit", None)
            if callable(emit):
                emit(Notice.from_invocation(
                    LifecyclePhase.INVOKE_REJECTED, inv, payload={"reason": reason},
                ))
        self._notify("invoke_rejected", {"reason": reason, "session_id": inv.session_id})

    def _reject(self, inv: Invocation, reason: str) -> Result:
        """终止类拒绝：通知 + 失败 Result（不调 core）。"""
        self._emit_rejected(inv, reason)
        return Result.failure("INVOKE_REJECTED", reason)

    # ---- 前置放行（各领域策略以各自领域方法判定，无统一协议）----
    async def _preflight(self, inv: Invocation) -> Result | None:
        """前置放行判定：返回非 None 即终止类拒绝短路；降级类写 fault 后放行。"""
        # 运维紧急开关：置位即终止拒绝。
        if self._kill_switch is not None and self._kill_switch.is_tripped(inv.session_id):
            return self._reject(inv, "killswitch")
        # 循环看门狗：命中重复/A-B 交替即终止拒绝。
        if self._watchdog is not None and self._watchdog.detect(inv):
            return self._reject(inv, "watchdog:loop")
        # 幂等去重：窗口内重复副作用即终止拒绝。
        if self._idempotency is not None:
            key = _idem_key(inv)
            if key is not None and self._idempotency.check_and_mark(key):
                return self._reject(inv, "duplicate_side_effect")
        # 限流许可：REJECT 终止；DEGRADE 写 fault 交降级选级。
        if self._rate_limit is not None:
            permit = self._rate_limit.acquire()
            if permit is RatePermit.REJECT:
                return self._reject(inv, "rate_limited")
            if permit is RatePermit.DEGRADE:
                inv.write_fault(FaultRecord(
                    reason="rate_limited", failed_target=failed_target_of(inv),
                ))
        # 熔断许可：OPEN/半开超额写 fault 交降级选级。
        if self._breaker is not None:
            reason = await self._breaker.permit(inv)
            if reason is not None:
                inv.write_fault(FaultRecord(reason=reason, failed_target=failed_target_of(inv)))
        return None

    # ---- 预算内中心调用（超时/重试/舱位，各有领域方法）----
    async def _budgeted_call(self, inv: Invocation) -> Result:
        """按超时→重试→舱位顺序包裹 core；熔断结果记数（含异常）。"""
        async def base(i: Invocation) -> Result:
            return await _await_result(self._core(i))

        call: Callable[[Invocation], Awaitable[Result]] = base
        # 超时最内层（紧贴 core），重试居中，舱位最外层——与原装配顺序等价。
        if self._timeout is not None:
            timeout = self._timeout
            inner = call
            async def call(i: Invocation, _p: _InvCall = inner, _t: Timeout = timeout) -> Result:
                return await _t.run_with_deadline(i, _p)
        if self._retry is not None:
            retry = self._retry
            inner = call
            async def call(i: Invocation, _p: _InvCall = inner, _r: RetryPolicy = retry) -> Result:
                return await _r.run_with_retries(i, _p)
        if self._bulkhead is not None:
            bulkhead = self._bulkhead
            inner = call
            async def call(i: Invocation, _p: _InvCall = inner, _b: Bulkhead = bulkhead) -> Result:
                return await _b.run_in_slot(i, _p)
        # 函数式变换器（extra_plugins）：Invocation 层最外层包裹。
        for transform in self._transformers:
            call = transform(call)

        if self._breaker is not None:
            try:
                result = await call(inv)
            except Exception:
                await self._breaker.observe_outcome(
                    inv, Result.failure("INVOKE_EXCEPTION", "invoke raised"),
                )
                raise  # 异常原样抛回，交降级链处理（不吞异常）
            await self._breaker.observe_outcome(inv, result)
            return result
        return await call(inv)

    # ---- 失败降级选级（领域决策，保四坑）----
    def _apply_recovery(self, inv: Invocation) -> str:
        """降级选级：读 fault → Degradation.select（四坑）→ 写 typed target。"""
        if self._degradation is None:
            return ""
        fault = inv.fault()
        if fault is None:
            fault = FaultRecord(reason="invoke_failed", failed_target=failed_target_of(inv))
        target = self._degradation.select(fault, session_id=inv.session_id)
        inv.write_routing_target(RoutingTarget(target=target, reason=fault.reason))
        self._emit_rejected(inv, fault.reason)
        return target

    async def _invoke(self, inv: Invocation) -> Result:
        """一次可靠调用：前置 → 预算内执行 → 失败降级 + 重路由。"""
        refusal = await self._preflight(inv)
        if refusal is not None:
            return refusal

        # 前置降级类 fault（限流/熔断）：调 core 前先选级写 typed target，
        # 让中心调用直接用降级目标（保 B1/B2）。
        if inv.fault() is not None and inv.routing_target() is None:
            target = self._apply_recovery(inv)
            if target and target != _REFUSE_TARGET:
                inv.meta.pop("fault", None)  # 已消费，避免重路由重复裁决
            elif target == _REFUSE_TARGET:
                return Result.failure("REFUSED", "degrade ladder exhausted; refusal")

        if self._degradation is not None:
            try:
                result = await self._budgeted_call(inv)
            except Exception as exc:
                # 配了降级策略：中心异常折叠为失败 Result + 具名 fault，
                # 交降级链选级切 backup（旧容器行为等价）；无降级策略则保持原样抛回。
                inv.write_fault(FaultRecord(
                    reason="invoke_exception", failed_target=failed_target_of(inv),
                ))
                result = Result.failure("INVOKE_EXCEPTION", str(exc))
        else:
            result = await self._budgeted_call(inv)
        for _ in range(_MAX_REROUTE):
            if result.ok:
                break
            current = inv.routing_target()
            existing_fault = inv.fault()
            if current is not None and current.target != _REFUSE_TARGET:
                # 上一轮已有降级目标且仍失败：把它记为新的失败目标继续选级。
                inv.write_fault(FaultRecord(
                    reason=(existing_fault.reason if existing_fault else "reroute_failed"),
                    failed_target=current.target,
                ))
            elif existing_fault is None:
                break
            inv.meta.pop("routing_target", None)
            target = self._apply_recovery(inv)
            if not target:
                break
            inv.meta.pop("fault", None)
            if target == _REFUSE_TARGET:
                result = Result.failure("REFUSED", "degrade ladder exhausted; refusal")
                break
            result = await self._budgeted_call(inv)
        if not result.ok:
            reason = result.error.message if result.error else "invoke failed"
            self._emit_rejected(inv, reason)
        return result

    def as_call(self) -> CallFn:
        """产出符合 R1 CallFn 签名的 call（构造 Invocation，R1 零感知）。"""
        async def adapter(effect: Effect, ctx: object) -> Result:
            return await self._invoke(_as_invocation(effect, ctx))
        return adapter

    async def run_invocation(self, inv: Invocation) -> Result:
        """直接对给定 Invocation 跑完整链（测试入口）。"""
        return await self._invoke(inv)
