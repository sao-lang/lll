"""组合层 call 装配器（规格 P2/P3）：承接原 AOPContainer 的编排能力。

AOP 概念取消，但"③call 装饰器链 + ②守卫 check/observe + dispatch + 链外降级重路由"
的编排逻辑必须保留——它是四坑语义的宿主。本模块把该逻辑归位到组合层，
插件（CircuitBreaker/RetryPolicy/Degradation 等）通过 duck-typing 复用其方法，
行为与原 AOPContainer 完全等价（差分对跑：全量测试不变）。

对外 API：
    - CompositeCall：可注册守卫/装饰器/裁决器的 call 装配器；
    - as_call()：产出 R1 注入签名 (effect, ctx) -> Result；
    - run_invocation(ctx)：测试入口。
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from runtime import Effect, Invoke, Result

from .call import Guard
from .context import (
    FAULT_KEY,
    ROUTING_TARGET_KEY,
    CallContext,
    FaultRecord,
    Invocation,
    Next,
    Verdict,
)

__all__ = ["CompositeCall"]

_REFUSE_TARGET = "refuse"
"""末级拒答目标名（与 R3 DegradeMode.REFUSE / R4 REFUSE_TARGET 对齐）。"""
_MAX_REROUTE = 8
"""降级重路由硬上限（防 ladder 成环或异常配置导致死循环）。"""


async def _await_result(outcome: Result | Awaitable[Result]) -> Result:
    """统一等待 core 的同步/异步返回（具名边界，避免 Any 泄漏）。"""
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


@dataclass(frozen=True)
class _SessionView:
    """Session 只读投影（从 ctx.session_id 派生，避免依赖完整 Session 对象）。"""
    session_id: str


def _as_ctx(effect: Effect, ctx: object) -> CallContext:
    """由 (effect, ctx) 构造 CallContext；meta 与 ctx.meta 同源（B3 状态统一）。"""
    ctx_meta = getattr(ctx, "meta", None)
    shared: dict[str, object] = ctx_meta if isinstance(ctx_meta, dict) else {}
    return CallContext(
        effect=effect, ctx=ctx,
        session=_SessionView(session_id=str(getattr(ctx, "session_id", "") or "")),
        meta=shared,
    )


class CompositeCall:
    """组合层 call 装配器：按角色注册插件、固定顺序编排、链外降级重路由。

    插件角色（duck-typing，方法名沿用已被规格取消的旧称以复用既有语义）：
        prelude.run / warden.guard / envelope.wrap / refiner.refine / salvage.recover。
    这些是组合层内部的编排槽位，不再是"AOP 五角色框架概念"（D-3）。
    """

    __slots__ = (
        "_notify", "_core", "_bus", "_envelopes", "_preludes",
        "_refiners", "_salvages", "_wardens", "registry",
    )

    def __init__(
        self,
        core: Callable[[Invocation], Result | Awaitable[Result]],
        *,
        registry: object | None = None,
        notify: Callable[[str, Mapping[str, object]], None] | None = None,
        bus: object | None = None,
    ) -> None:
        """装配：core 是中心 Port 调用（如 ModelGateway.as_call）。

        Args:
            core: 中心调用 (invocation) -> Result；接收 CallContext 以读 typed routing_target。
            registry: 共用 Provider 注册表（可选）。
            notify: ④观察回调（invoke_rejected 等相位），缺省 no-op。
            bus: 可选 LifecycleBus（含 emit(Notice)）；传入时 invoke_rejected 经它广播。
        """
        self.registry = registry
        self._core = core
        self._bus = bus
        self._notify = notify or (lambda phase, payload: None)
        self._preludes: list[Any] = []
        self._wardens: list[Any] = []
        self._envelopes: list[Any] = []
        self._refiners: list[Any] = []
        self._salvages: list[Any] = []

    # ---- 注册入口（返回 self 支持链式）----
    def add_prelude(self, p: Any) -> CompositeCall:
        """注册前导包裹器。"""
        self._preludes.append(p)
        return self

    def add_warden(self, p: Any) -> CompositeCall:
        """注册准入守卫（②Guard 的 check 语义，沿用 guard 方法名复用插件）。"""
        self._wardens.append(p)
        return self

    def add_envelope(self, p: Any) -> CompositeCall:
        """注册环绕装饰器（③call 装饰器语义，沿用 wrap 方法名复用插件）。"""
        self._envelopes.append(p)
        return self

    def add_refiner(self, p: Any) -> CompositeCall:
        """注册 payload 改写器（map_effect 语义，沿用 refine 方法名）。"""
        self._refiners.append(p)
        return self

    def add_salvage(self, p: Any) -> CompositeCall:
        """注册降级裁决器（②降级守卫，沿用 recover 方法名）。"""
        self._salvages.append(p)
        return self

    def register(self, role: Any, p: object) -> CompositeCall:
        """按角色字符串注册（兼容装配期从配置读到的 str）。"""
        name = getattr(role, "value", role)
        adders: dict[str, Callable[[object], CompositeCall]] = {
            "prelude": self.add_prelude, "warden": self.add_warden,
            "envelope": self.add_envelope, "refiner": self.add_refiner,
            "salvage": self.add_salvage,
        }
        if name not in adders:
            raise ValueError(f"unknown call role: {role}")
        return adders[name](p)

    def _emit_rejected(self, inv: CallContext, reason: str) -> None:
        """发 invoke_rejected（④notify，每次 Invocation 只发一次，meta 标记去重）。"""
        if inv.meta.get("_rejected_emitted"):
            return
        inv.meta["_rejected_emitted"] = True
        if self._bus is not None:
            # 经 LifecycleBus 广播（兼容既有 bus 订阅测试）；Notice 与 CallContext 同源。
            from compose import LifecyclePhase, Notice  # 延迟导入避免循环
            emit = getattr(self._bus, "emit", None)
            if callable(emit):
                emit(Notice.from_invocation(LifecyclePhase.INVOKE_REJECTED, inv,
                                            payload={"reason": reason}))
        self._notify("invoke_rejected", {"reason": reason, "session_id": inv.session_id})

    async def _run_wardens(self, inv: CallContext) -> Verdict | None:
        """依次问守卫；首个 allowed=False 短路返回。"""
        for w in self._wardens:
            verdict = cast(Verdict, await w.guard(inv))
            if not verdict.allowed:
                return verdict
        return None

    async def _apply_salvage(self, inv: CallContext, exc: Exception | None) -> Result | None:
        """依次问裁决器，第一个产出非空结果即采纳；兜底触发即发 invoke_rejected。"""
        for s in self._salvages:
            result = await s.recover(exc or RuntimeError("invoke failed"), inv)
            if result is not None:
                fault = inv.fault()
                reason = fault.reason if fault is not None else (str(exc) if exc else "invoke rejected")
                self._emit_rejected(inv, reason)
                return cast(Result, result)
        return None

    async def _center(self, inv: CallContext) -> Result:
        """中心调用：refiner 改写 → core → 失败且带 fault 未裁决则 salvage。"""
        for r in self._refiners:
            inv = await r.refine(inv)
        try:
            result = await _await_result(self._core(inv))
        except Exception as exc:  # 中心异常进 salvage
            salvaged = await self._apply_salvage(inv, exc)
            if salvaged is None:
                raise
            return salvaged
        if not result.ok and inv.fault() is not None and inv.routing_target() is None:
            salvaged = await self._apply_salvage(inv, None)
            if salvaged is not None:
                return salvaged
        return result

    async def _guard_phase(self, inv: CallContext, inner: Next) -> Result:
        """准入层：终止类拒绝短路；降级类（写 fault）裁决后继续。"""
        rejection = await self._run_wardens(inv)
        has_fault = inv.fault() is not None
        if rejection is not None and not has_fault:
            self._emit_rejected(inv, rejection.reason)
            return Result.failure("INVOKE_REJECTED", rejection.reason)
        if has_fault:
            await self._apply_salvage(inv, None)
            inv.meta.pop(FAULT_KEY, None)
        return await inner(inv)

    def _build_chain(self, *, include_wardens: bool = True) -> Next:
        """构建 call 链：③call 装饰器（envelope 适配）→ ②守卫（Guard 适配）→ core。

        生产路径真实经过组合层原语（规格 P2/P3，修复 B1）：
        - envelope 插件经 `envelope_decorator` 适配为 ③CallFn 装饰器；
        - warden 插件经 `WardenGuardAdapter` 适配为 ②Guard；
        - 两者由 `build_call` 组装（守卫顺序 + 装饰器链），不再是私有五角色洋葱。

        重跑时 include_wardens=False（跳过一次性守卫，避免幂等自命中，保四坑）。
        prelude/refiner 保留为内部槽位（⑥组件形态），不影响 ②③ 主链。
        """
        from .adapters import WardenGuardAdapter, envelope_decorator
        from .call import build_call

        # 中心 CallFn：以 CallContext 为 ctx，调 _center（含 refiner + core + salvage 入口）
        async def base(effect: Effect, ctx: object) -> Result:
            inv = ctx if isinstance(ctx, CallContext) else _as_ctx(effect, ctx)
            return await self._center(inv)

        # ③call 装饰器：envelope 插件适配（后注册在内层，故 reversed）
        decorators = [envelope_decorator(e) for e in reversed(self._envelopes)]
        # ②守卫：单个 Guard 承载完整 Warden 阶段语义（拒绝短路 + fault 放行 + 通知）。
        # 由 build_call 的 with_guard 驱动——生产真实经过 Guard.check/observe 协议。
        guards: list[Guard] = [self._warden_phase_guard()] if include_wardens and self._wardens else []

        # 用组合层 build_call 组装（生产真实走 Guard/装饰器/CallFn）
        call = build_call(base, decorators=decorators, guards=guards)

        async def chain(i: CallContext) -> Result:
            return await call(i.effect, i)
        return self._with_preludes(chain)

    def _warden_phase_guard(self) -> Guard:
        """把 Warden 准入阶段封装为 compose.Guard（走 with_guard，保四坑语义）。"""
        owner = self

        class _WardenPhaseGuard:
            name = "r3_warden_phase"
            reject_code = "INVOKE_REJECTED"  # 终止类拒绝对外码（行为等价原容器）

            async def check(self, effect: Effect, ctx: object) -> Verdict | None:
                inv = ctx if isinstance(ctx, CallContext) else _as_ctx(effect, ctx)
                rejection = await owner._run_wardens(inv)
                has_fault = inv.fault() is not None
                if rejection is not None and not has_fault:
                    owner._emit_rejected(inv, rejection.reason)
                    return Verdict(allowed=False, reason=rejection.reason)
                if has_fault:
                    # 降级类 fault 预裁决（原 _guard_phase 语义），清标记避免链外重复消费
                    await owner._apply_salvage(inv, None)
                    inv.meta.pop(FAULT_KEY, None)
                return None

            async def observe(self, effect: Effect, result: Result, ctx: object) -> None:
                # 空操作：CircuitBreaker 等后置记数由 Envelope（③call 装饰器）的 wrap 承载，
                # 原容器 warden 阶段不调 observe——此处若再调会 double-record（复审 S1）。
                return None

        return _WardenPhaseGuard()

    def _with_preludes(self, inner: Next) -> Next:
        """最外层包 Prelude（⑥组件槽位，保留原行为）。"""
        chain: Next = inner
        for p in reversed(self._preludes):
            prelude = p
            pre_inner = chain
            async def pre_layer(i: CallContext, _p: Any = prelude, _inner: Next = pre_inner) -> Result:
                return cast(Result, await _p.run(i, _inner))
            chain = pre_layer
        return chain

    async def _call(self, inv: CallContext) -> Result:
        """跑完整链 + 链外 Salvage 重路由（四坑宿主，行为等价原 AOPContainer）。"""
        chain = self._build_chain()
        reroute_chain = self._build_chain(include_wardens=False)
        result = await chain(inv)
        for _ in range(_MAX_REROUTE):
            if result.ok:
                break
            current = inv.routing_target()
            existing_fault = inv.fault()
            if current is not None and current.target != _REFUSE_TARGET:
                inv.write_fault(FaultRecord(
                    reason=(existing_fault.reason if existing_fault else "reroute_failed"),
                    failed_target=current.target,
                ))
            elif existing_fault is None:
                break
            inv.meta.pop(ROUTING_TARGET_KEY, None)
            salvaged = await self._apply_salvage(inv, None)
            if salvaged is None:
                break
            inv.meta.pop(FAULT_KEY, None)
            new_target = inv.routing_target()
            if new_target is not None and new_target.target == _REFUSE_TARGET:
                result = salvaged
                break
            result = await reroute_chain(inv)
        if not result.ok:
            reason = result.error.message if result.error else "invoke failed"
            self._emit_rejected(inv, reason)
        return result

    async def run_invocation(self, inv: CallContext) -> Result:
        """直接对给定 CallContext 跑完整链（测试入口）。"""
        return await self._call(inv)

    def as_call(self) -> Callable[[Effect, object], Awaitable[Result]]:
        """产出符合 R1 CallFn 签名的 call（构造 CallContext，R1 零感知）。

        状态容器统一（规格 P2/B3）：CallContext.meta 直接复用 R1 传入 ctx.meta
        的同一 dict 引用——守卫写入的 typed target 与网关从 inv.meta 读取的
        是同一份，避免双轨状态导致降级重路由静默失效。
        """
        async def adapter(effect: Effect, ctx: object) -> Result:
            ctx_meta = getattr(ctx, "meta", None)
            shared_meta: dict[str, object] = ctx_meta if isinstance(ctx_meta, dict) else {}
            inv = CallContext(
                effect=effect,
                ctx=ctx,
                session=_SessionView(session_id=str(getattr(ctx, "session_id", "") or "")),
                meta=shared_meta,
            )
            return await self._call(inv)
        return adapter
