"""AOP 容器：按角色注册增强器，按固定顺序编排中心调用。

职责：
- 持有共用 Registry（与 LifecycleBus/装配层同源，P6）。
- as_call() 产出符合 Runner CallFn 签名的适配器，构造 Invocation 并跑增强链。
- 固定顺序：Prelude → Warden → Envelope(前) → Refiner → Port call → Envelope(后) → Salvage。
- 降级意图：Warden/Envelope 写 inv.meta["fault"]，容器调 Salvage（Degradation）选级
  写 typed routing_target 后**重路由继续**（本次 Effect 用备用后端，回归门禁断言）。
  终止类拒绝（无 fault）发 invoke_rejected 通知并短路。
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hooks.lifecycle import LifecycleBus, LifecyclePhase, Notice
from runtime import Effect, Result

from .invocation import (
    FAULT_KEY,
    ROUTING_TARGET_KEY,
    FaultRecord,
    Invocation,
    Next,
    Verdict,
)
from .protocols import Envelope, Prelude, Refiner, Role, Salvage, Warden

__all__ = ["AOPContainer"]

_MAX_REROUTE = 8
"""降级重路由硬上限（防 ladder 有环或异常配置导致死循环；正常阶梯远小于此）。"""
_REFUSE_TARGET = "refuse"
"""末级拒答目标名（与 R3 DegradeMode.REFUSE / R4 REFUSE_TARGET 对齐，容器不反向依赖 harness）。"""


async def _await_result(outcome: Result | Awaitable[Result]) -> Result:
    """统一等待 core 的同步/异步返回（具名边界，避免 Any 泄漏）。"""
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


@dataclass(frozen=True)
class _SessionView:
    """Session 的只读投影（容器从 ctx.session_id 派生，避免依赖完整 Session 对象）。"""
    session_id: str


class AOPContainer:
    """按角色注册增强器并固定顺序编排；共用 Registry；降级裁决接 Salvage。"""

    __slots__ = (
        "_bus",
        "_core",
        "_envelopes",
        "_preludes",
        "_refiners",
        "_salvages",
        "_wardens",
        "registry",
    )

    def __init__(
        self,
        core: Callable[[Invocation], Result | Awaitable[Result]],
        *,
        registry: object | None = None,
        bus: LifecycleBus | None = None,
    ) -> None:
        """装配容器：core 是中心 Port 调用（如 ModelGateway.as_call）。

        参数：
            core: 中心调用 (invocation) -> Result | Awaitable[Result]。
                接收 Invocation 以便从 inv.meta 读 typed routing_target。
            registry: 共用 Provider 注册表（P6，缺省 None 由装配层注入）。
            bus: Lifecycle 总线（用于发 invoke_rejected 通知；缺省 None 不发）。
        """
        self.registry = registry
        self._core = core
        self._bus = bus
        self._preludes: list[Prelude] = []
        self._wardens: list[Warden] = []
        self._envelopes: list[Envelope] = []
        self._refiners: list[Refiner] = []
        self._salvages: list[Salvage] = []

    # ---- 注册入口（R11 文档引用这几个方法名）----
    def add_prelude(self, p: Prelude) -> AOPContainer:
        """注册前导增强器（返回 self 支持链式）。"""
        self._preludes.append(p)
        return self

    def add_warden(self, p: Warden) -> AOPContainer:
        """注册守卫增强器（返回 self 支持链式）。"""
        self._wardens.append(p)
        return self

    def add_envelope(self, p: Envelope) -> AOPContainer:
        """注册环绕增强器（返回 self 支持链式）。"""
        self._envelopes.append(p)
        return self

    def add_refiner(self, p: Refiner) -> AOPContainer:
        """注册精炼增强器（返回 self 支持链式）。"""
        self._refiners.append(p)
        return self

    def add_salvage(self, p: Salvage) -> AOPContainer:
        """注册兜底增强器（返回 self 支持链式）。"""
        self._salvages.append(p)
        return self

    def register(self, role: Role | str, p: object) -> AOPContainer:
        """按角色注册（装配期从 extra_plugins 分派用）。

        参数：
            role: Role 成员或等值字符串（装配层从配置读到的仍是 str，故兼容）。
            p: 增强器实例（须有 name）。
        异常：
            ValueError: 未知角色。
        """
        try:
            r = role if isinstance(role, Role) else Role(role)
        except ValueError as exc:
            raise ValueError(f"unknown aop role: {role}") from exc
        adders: dict[Role, Callable[[Any], AOPContainer]] = {
            Role.PRELUDE: self.add_prelude,
            Role.WARDEN: self.add_warden,
            Role.ENVELOPE: self.add_envelope,
            Role.REFINER: self.add_refiner,
            Role.SALVAGE: self.add_salvage,
        }
        return adders[r](p)

    def _emit_rejected(self, inv: Invocation, reason: str) -> None:
        """发 invoke_rejected 通知（堵审计盲区；异常隔离由总线保证）。

        每次 Invocation 只发一次（meta 具名标记去重），覆盖 Warden 拒绝、
        Salvage 兜底、终止类失败 Result 三条出口。
        """
        if self._bus is None or inv.meta.get("_rejected_emitted"):
            return
        inv.meta["_rejected_emitted"] = True
        notice = Notice.from_invocation(
            LifecyclePhase.INVOKE_REJECTED, inv, payload={"reason": reason},
        )
        self._bus.emit(notice)

    async def _run_wardens(self, inv: Invocation) -> Verdict | None:
        """依次问守卫；首个 allowed=False 短路返回，降级类故障已写入 inv.meta。

        返回：
            拒绝 Verdict（有守卫拒绝），或 None（全部放行）。
        """
        for w in self._wardens:
            verdict = await w.guard(inv)
            if not verdict.allowed:
                return verdict
        return None

    async def _apply_salvage(self, inv: Invocation, exc: Exception | None) -> Result | None:
        """依次问 Salvage，第一个产出非空结果即采纳（Degradation 唯一裁决者）。

        兜底被触发即代表本次 invoke 遭遇拒绝/异常/降级——强制发 invoke_rejected
        通知堵审计盲区（规格 P3）。终止类 Warden 拒绝在 _guard_phase 已单独发，
        不走本方法，故不会重复。
        """
        for s in self._salvages:
            result = await s.recover(exc or RuntimeError("invoke failed"), inv)
            if result is not None:
                fault = inv.fault()
                reason = fault.reason if fault is not None else (str(exc) if exc else "invoke rejected")
                self._emit_rejected(inv, reason)
                return result
        return None

    async def _center(self, inv: Invocation) -> Result:
        """中心调用：Refiner 改写 → core（含内部 AOP 前序已完成）→ 失败则 Salvage。

        分派：
            - Refiner 逐个改写 Invocation（返回新对象）。
            - core 产出 Result；若 core 抛异常，包成 Exception 传 Salvage。
            - core 返回失败 Result 且 inv 有 fault 记录 → 触发 Salvage 选级。
        """
        for r in self._refiners:
            inv = await r.refine(inv)
        try:
            result = await _await_result(self._core(inv))
        except Exception as exc:  # 中心异常进 Salvage
            salvaged = await self._apply_salvage(inv, exc)
            if salvaged is None:
                raise
            return salvaged
        # 失败 Result 且带 fault 且尚未裁决过 → 说明 fault 是 core 内部 Envelope 新写的
        # （如 retry 耗尽/timeout），此时才需 Salvage 裁决。若 routing_target 已存在，
        # 说明 Warden 阶段已预裁决过，core 返回的 REFUSED 等失败就是最终结果，不重复兜底。
        if not result.ok and inv.fault() is not None and inv.routing_target() is None:
            salvaged = await self._apply_salvage(inv, None)
            if salvaged is not None:
                return salvaged
        return result

    async def _guard_phase(self, inv: Invocation, next: Next) -> Result:
        """Warden 准入层（Prelude 之内、Envelope 之外）。

        终止类拒绝（无 fault）→ invoke_rejected + 短路；降级类（写 fault）→ 裁决后继续。
        """
        rejection = await self._run_wardens(inv)
        has_fault = inv.fault() is not None
        if rejection is not None and not has_fault:
            self._emit_rejected(inv, rejection.reason)
            return Result.failure("INVOKE_REJECTED", rejection.reason)
        if has_fault:
            # Warden 阶段预裁决：Degradation 选级写 typed routing_target，
            # 本次 Effect 用备用后端继续。清 fault 标记已裁决，避免链外重复消费同一故障。
            await self._apply_salvage(inv, None)
            inv.meta.pop(FAULT_KEY, None)
        return await next(inv)

    @staticmethod
    def _envelope_layer(e: Envelope, inner: Next) -> Next:
        """构造一个 Envelope 洋葱层（工厂函数，类型明确）。"""
        async def layer(i: Invocation) -> Result:
            """单个 Envelope 层：wrap(inv, next) 包裹下一层。"""
            return await e.wrap(i, inner)
        return layer

    @staticmethod
    def _prelude_layer(p: Prelude, inner: Next) -> Next:
        """构造一个 Prelude 洋葱层（工厂函数，类型明确）。"""
        async def layer(i: Invocation) -> Result:
            """单个 Prelude 层：run(inv, next) 包裹下一层。"""
            return await p.run(i, inner)
        return layer

    def _build_chain(self, *, include_wardens: bool = True) -> Next:
        """构建增强洋葱链：Prelude(外) → Warden → Envelope → Refiner → Port(内)。

        参数：
            include_wardens: 是否包含 Warden 层。降级重路由重跑时传 False——
                状态型 Warden（幂等/看门狗）的一次性判定在首次已执行，切换后端
                不是新的业务请求，重跑不得再次拦截（否则幂等键自命中、降级失效）。
        返回：
            链条入口（收 Invocation 产 Result）。
        """
        async def core_next(i: Invocation) -> Result:
            """Envelope 洋葱链的中心：进入 Refiner → Port。"""
            return await self._center(i)

        next_fn: Next = core_next

        # Envelope 由内向外包裹（后注册的在内层，先注册的最外层）。
        for e in reversed(self._envelopes):
            next_fn = self._envelope_layer(e, next_fn)

        if include_wardens:
            # Warden 层：包在 Envelope 之外、Prelude 之内。
            warden_inner = next_fn

            async def warden_layer(i: Invocation) -> Result:
                """Warden 准入层（顺序上在 Envelope 之前执行）。"""
                return await self._guard_phase(i, warden_inner)

            next_fn = warden_layer

        # Prelude 最外层包裹整条链。
        for p in reversed(self._preludes):
            next_fn = self._prelude_layer(p, next_fn)

        return next_fn

    async def _call(self, inv: Invocation) -> Result:
        """跑完整增强链 + 链外 Salvage 兜底重路由（规格 §3 顺序 ...Envelope后→Salvage）。

        关键（P9 R3-7）：Retry/Timeout 作为 Envelope 在 `await next()` 返回后才把
        fault 写进 meta，只有整条链返回后才知道最终失败目标。故把降级裁决放在
        链外——有未裁决 fault 且无 routing_target 时，Degradation 选级写 typed 目标，
        清空 fault 后重跑，网关据新目标切后端。阶梯逐级收敛，_MAX_REROUTE 兜底防死循环。
        """
        chain = self._build_chain()  # 首跑：完整链（含 Warden 准入）
        reroute_chain = self._build_chain(include_wardens=False)  # 重跑：跳过一次性 Warden
        result = await chain(inv)
        for _ in range(_MAX_REROUTE):
            if result.ok:
                break
            current = inv.routing_target()
            existing_fault = inv.fault()
            if current is not None and current.target != _REFUSE_TARGET:
                # 当前目标已裁决过且又失败：故障目标就是它本身（逐级剔除的关键）。
                # 不依赖 Envelope 的 failed_target_of（它可能仍指向初始 primary）。
                inv.write_fault(FaultRecord(
                    reason=(existing_fault.reason if existing_fault else "reroute_failed"),
                    failed_target=current.target,
                ))
            elif existing_fault is None:
                break  # 无 fault 且无目标：普通失败，无可裁决
            # Degradation 选级：剔除 failed_target（本会话 available 永久写回），写新 routing_target。
            inv.meta.pop(ROUTING_TARGET_KEY, None)
            salvaged = await self._apply_salvage(inv, None)
            if salvaged is None:
                break  # 无 Salvage：故障无裁决者，返回失败 Result
            inv.meta.pop(FAULT_KEY, None)  # 标记本轮故障已裁决
            new_target = inv.routing_target()
            if new_target is not None and new_target.target == _REFUSE_TARGET:
                result = salvaged  # 末级拒答：不调后端，真拒答由网关产生
                break
            result = await reroute_chain(inv)  # 重跑（跳过 Warden；网关读新 routing_target 切后端）
        # 统一失败出口：终止类失败 Result（如 BULKHEAD_FULL/IDEMPOTENCY_REQUIRED/
        # INVALID_EFFECT）也发 invoke_rejected；_emit_rejected 内部按 meta 标记去重，
        # Warden 拒绝/Salvage 兜底已发过则不重复。
        if not result.ok:
            reason = result.error.message if result.error else "invoke failed"
            self._emit_rejected(inv, reason)
        return result

    async def run_invocation(self, inv: Invocation) -> Result:
        """直接对给定 Invocation 跑增强链（装配层/测试入口）。"""
        return await self._call(inv)

    def as_call(self) -> Callable[[Effect, object], Any]:
        """产出符合 Runner CallFn 签名的适配器。

        返回：
            async (effect, ctx) -> Result。构造 Invocation（session 投影 + 每次新建 meta），
            跑增强链；Runner 无需感知 AOP 存在。
        """
        async def adapter(effect: Effect, ctx: object) -> Result:
            """Runner call 槽适配器：Invocation 构造 → 增强链 → Result。"""
            inv = Invocation(
                effect=effect,
                ctx=ctx,
                session=_SessionView(session_id=str(getattr(ctx, "session_id", "") or "")),
                meta={},
            )
            return await self._call(inv)

        return adapter
