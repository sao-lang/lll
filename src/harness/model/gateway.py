"""R4 模型网关：编排 缓存→路由→调用→结构化校验/修复→成本归因。

上层唯一入口 ModelGateway.complete()；换 LLMPort 实现时上层零改动（R4 §8 验收 3）。
成本归因只记录 LLMPort 响应里的 usage，不自行估算；CostGuard 保证不绕过预算。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NoReturn, cast

from compose.context import Invocation, RoutingTarget
from compose.guards import CallState
from harness._ctxview import as_port_ctx, coerce_port_ctx
from harness.model.ports import LLMPortWithUsage
from compose import (
    LLMPort,
    LLMRequest,
    LLMResponse,
    LifecycleBus,
    LifecyclePhase,
    Notice,
    PortCtx,
    StreamEvent,
    StreamEventKind,
    ToolSpec,
    Usage,
)
from runtime import Effect, Invoke, Message, PortName, Result

from .cache import CachePort, ExactCache
from .registry import ModelRegistry
from .router import NO_DEFAULT, ModelRouter, RuleRouter
from .stream import has_stream_events, wrap_text_stream
from .structured import SchemaValidator, StructuredOutputValidator
from .types import (
    REFUSED_CODE,
    TARGET_UNRESOLVED_CODE,
    BackendResolution,
    CostAttributor,
    CostGuard,
    CostRecord,
    RefuseExecution,
    Reservation,
    StructuredOutputSpec,
    TargetUnresolved,
    cache_key_string,
)

__all__ = ["ModelGateway"]

# R4 本地拒绝目标常量（规则 5）：值与 R3 DegradeMode.REFUSE 对齐，
# 但网关不依赖 R3，故在本层用具名常量替代魔法串，比较时右侧是常量名而非字符串字面量。
REFUSE_TARGET = "refuse"

# 无定价信息时的保守单次预估（美元）。用户可通过 cost_estimator 注入精确单价
# （如接 router.cost_tiers / 模型定价表）；此值偏保守，宁可少放也不超支。
_DEFAULT_ESTIMATED_COST_USD = 0.01


def _default_cost_estimate(request: LLMRequest) -> float:
    """默认成本预估：有 max_tokens 时按 1e-5 USD/token 估，否则用保守常量。"""
    if request.max_tokens is not None and request.max_tokens > 0:
        return max(_DEFAULT_ESTIMATED_COST_USD, request.max_tokens * 1e-5)
    return _DEFAULT_ESTIMATED_COST_USD


class ModelGateway:
    """模型网关：可插拔地组合 LLMPort + 路由 + 缓存 + 结构化校验 + 成本账本。

    属性：
        ledger: 成本账本（外部可查）。cache: 缓存实例。
    """

    def __init__(
        self,
        *,
        llm: LLMPortWithUsage,
        router: ModelRouter | None = None,
        cache: CachePort | None = None,
        spec: StructuredOutputSpec | None = None,
        validator: SchemaValidator | None = None,
        prompt_version: str = "current",
        cost_guard: CostGuard | None = None,
        registry: ModelRegistry | None = None,
        cost_estimator: Callable[[LLMRequest], float] | None = None,
        bus: LifecycleBus | None = None,
    ) -> None:
        """装配网关：只 llm 必填，其余缺省用默认实现。

        参数：
            llm: 默认模型后端（实现 LLMPort.complete/stream）。
            router: 路由策略（缺省规则路由）——complete 时真调用，不再空转。
            cache: 缓存（缺省精确缓存）。
            spec: 结构化输出规格（None 表示不校验）。
            validator: 注入的结构化校验器（None 用 MinimalSchemaValidator）；
                仅在 spec 非空时生效，替换后校验与修复循环用同一实现。
            prompt_version: prompt 版本（进缓存键）。cost_guard: 成本硬闸。
            registry: 模型端点注册表（缺省 None=仅默认后端，兼容旧行为）。
            cost_estimator: 调用前成本预估函数（P2b：供 CostGuard.reserve 预留）；
                缺省用保守默认（max_tokens×1e-5 或 0.01 USD），可注入模型定价表。
        """
        self._llm = llm
        self._router = router or RuleRouter()
        self._cache = cache if cache is not None else ExactCache()
        self._registry = registry
        self._spec = spec
        self._validator = StructuredOutputValidator(spec, validator=validator) if spec else None
        self._prompt_version = prompt_version
        self._cost_guard = cost_guard
        self._cost_estimator = cost_estimator or _default_cost_estimate
        self._bus = bus  # Lifecycle 通知（before_model/after_model；缺省不发）
        self.ledger = CostAttributor()
        self.cache = self._cache
        # P2a singleflight：同一缓存 key 的并发 miss 共享一次后端调用，
        # 避免击穿（40 并发 → 真调 40 次）、重复记账。
        self._inflight: dict[str, asyncio.Future[LLMResponse]] = {}

    @property
    def registry(self) -> ModelRegistry | None:
        """装配核对用：网关解析降级目标所用的注册表（与共用容器同源）。"""
        return self._registry

    def attach_bus(self, bus: LifecycleBus) -> None:
        """装配期总线共享（幂等）：把 lifecycle 总线接进网关通知通道。

        为何存在：门面每次 say() 装配出全新 bus，而 ModelCapability.dispatch()
        自建 gateway 时构造参数 bus 缺省 None——不共享则 before_model/after_model
        永远 0 发射（hook 与事件流都看不到模型事件）。同总线才同真相。
        """
        if self._bus is not bus:
            self._bus = bus

    def _notify_model(self, phase: LifecyclePhase, request: LLMRequest, ctx: PortCtx,
                      *, extra: dict[str, object] | None = None) -> None:  # purity-exempt: object 为通知元数据动态边界（存量，行号位移）
        """发 model 相位通知（纯通知、异常隔离由总线保证，不拦截调用）。"""
        if self._bus is None:
            return
        self._bus.emit(Notice(
            phase=phase,
            session_id=ctx.session_id,
            port=PortName.LLM.value,
            payload={"model": request.model or "", **(extra or {})},
            meta=dict(ctx.meta),
        ))

    def _resolve_llm(
        self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None
    ) -> BackendResolution:
        """降级执行唯一决策点（R4 §2.2/§5.5）：返回三态，任何分支不回退默认模型。

        参数：
            request: LLM 请求。ctx: Port 上下文。
            degrade_target: 降级目标名（优先于 router 结果；来自 inv.meta 的 typed routing_target）。
        返回：
            LLMPort（切后端）/ RefuseExecution（拒答）/ TargetUnresolved（未注册显式失败）。
        说明：
            - router 总是被调用（修复 _router 空转），路由理由进 ctx.meta 供 trace。
            - REFUSE 优先于一切：不解析、不缓存、不调后端。
            - 非空未注册 target → TargetUnresolved（不再 raise ValueError 折叠成 PORT_CRASHED）。
            - registry 缺省时：无 target 走默认后端（兼容）；有 target 则 TargetUnresolved（不回退）。
        """
        # 构造 router 需要的 Invoke：task_hint 来自请求。
        effect = Invoke(
            port=PortName.LLM.value,
            payload={
                "messages": [dict(m) for m in request.messages],
                "model": request.model or "",
                "task_hint": request.task_hint or "",
            },
        )
        decision = self._router.route(effect, ctx)
        target = degrade_target or (decision.model if decision.model and decision.model != NO_DEFAULT else None)
        ctx.meta.setdefault("model_route", []).append(
            f"{decision.reason}->{target or 'default'}"
        )
        if target == REFUSE_TARGET:
            return RefuseExecution(reason="degraded to refusal; no backend invoked")
        if target is None:
            # 无目标：router 未显式指定 → 默认后端（设计允许的唯一下沉情形）。
            return self._llm
        resolved = self._registry.resolve_llm(target) if self._registry is not None else None
        if resolved is None:
            # 非空未注册目标：显式失败，绝不静默回退（降级红线）。
            return TargetUnresolved(target=target)
        return resolved

    @staticmethod
    def _resolution_error_response(resolution: RefuseExecution | TargetUnresolved) -> LLMResponse:
        """把终止型三态折叠为带稳定错误码前缀的 LLMResponse（as_call 再折为 Result）。

        error 的前缀即稳定码（REFUSED / TARGET_UNRESOLVED），与三态 to_result() 同源常量。
        """
        code = REFUSED_CODE if isinstance(resolution, RefuseExecution) else TARGET_UNRESOLVED_CODE
        return LLMResponse(
            content="", tool_calls=[], usage=Usage(),
            error=f"{code}: {resolution.reason if isinstance(resolution, RefuseExecution) else resolution.target}",
        )

    @staticmethod
    def _raise_resolution_error(resolution: RefuseExecution | TargetUnresolved) -> NoReturn:
        """流路径的终止型三态：抛带稳定码的 ValueError（经 runner 折叠为失败信封）。"""
        code = REFUSED_CODE if isinstance(resolution, RefuseExecution) else TARGET_UNRESOLVED_CODE
        detail = resolution.reason if isinstance(resolution, RefuseExecution) else resolution.target
        raise ValueError(f"{code}: {detail}")

    def _extract_target(self, ctx: object, inv: Invocation | None = None) -> str | None:  # purity-exempt: ctx 为 typed 降级动态边界（存量，行号位移）
        """从 typed 降级目标读 target（唯一来源；reason 只进 trace，不作 target）。

        P4 双入口兼容：
            - inv 给定：读 inv.routing_target()（可靠性执行器的 CallContext 路径）；
            - inv 缺省：从 ctx.meta 读 RoutingTarget（§2.5 dispatch 路径，经 CallState）。
        """
        routing = inv.routing_target() if inv is not None else CallState(ctx).routing_target()
        target = routing.target if routing is not None else None
        if routing is not None:
            # 规则 6 例外：meta 是 R1 RuntimeCtx 具名固定字段
            ctx_meta = getattr(ctx, "meta", None)
            if isinstance(ctx_meta, dict):
                ctx_meta.setdefault("degrade_reason", []).append(routing.reason)
        return target

    async def _center(self, effect: Effect, ctx: object, target: str | None) -> Result:  # purity-exempt: ctx 为 Effect 通用上下文边界（存量，行号位移）
        """模型中心调用（as_call / as_dispatch 共用的唯一实现）。"""
        if not isinstance(effect, Invoke):
            return Result.failure("INVALID_EFFECT", "model port needs Invoke effect")

        port_ctx = as_port_ctx(ctx)
        payload = effect.payload
        # 工具目录透传（B8 三链断裂修复）：arch 把 ToolSpec 序列化进 payload["tools"]，
        # 这里还原成 ToolSpec 写入 request.tools_catalog，adapter 才会发 tools 给 LLM。
        raw_tools = payload.get("tools") or []
        tools_catalog: list[ToolSpec] | None = None
        if isinstance(raw_tools, list) and raw_tools:
            tools_catalog = [
                t if isinstance(t, ToolSpec) else ToolSpec.model_validate(t)
                for t in raw_tools
            ]
        request = LLMRequest(
            messages=list(payload.get("messages", [])),
            model=str(payload.get("model") or "") or None,
            task_hint=str(payload.get("task_hint") or "") or None,
            tools_catalog=tools_catalog,
        )
        # 意图级联 task/tier/text 透传（U-1 关闭）：统一从 EffectPayload.meta 读
        # （R1 已知键，零改内核）；tier 供 RuleRouter 消费，intent_text 供审计。
        intent_meta = payload.get("meta") or {}
        if tier := str(intent_meta.get("tier") or ""):
            port_ctx.meta["tier"] = tier
        if intent_text := str(intent_meta.get("intent_text") or ""):
            port_ctx.meta["intent_text"] = intent_text
        self._notify_model(LifecyclePhase.BEFORE_MODEL, LLMRequest(
            messages=list(payload.get("messages", [])),
            model=str(payload.get("model") or "") or None,
        ), port_ctx)

        error_text = ""
        try:
            if target == REFUSE_TARGET:
                error_text = REFUSED_CODE
                return RefuseExecution("degraded to refusal; no backend invoked").to_result()
            response = await self.complete(request, port_ctx, degrade_target=target)
            error_text = response.error or ""
            if response.error:
                return Result.failure(response.error.split(":")[0], response.error)
            return Result.success({
                "content": response.content,
                "tool_calls": response.tool_calls,
                "usage": response.usage,
            })
        except BaseException as exc:
            error_text = error_text or f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._notify_model(LifecyclePhase.AFTER_MODEL, request, port_ctx,
                               extra={"error": error_text})

    def as_call(self) -> Callable[[Invocation], Awaitable[Result]]:
        """兼容入口（可靠性执行器 core）：Invocation → _center。

        降级目标从 inv.routing_target() 读（R3 typed，与 Registry 同源）。
        """
        async def adapter(inv: Invocation) -> Result:
            target = self._extract_target(inv.ctx, inv)
            return await self._center(inv.effect, inv.ctx, target)
        return adapter

    def as_dispatch(self) -> Callable[[Effect, object], Awaitable[Result]]:  # purity-exempt: dispatch ctx 动态边界（存量，行号位移）
        """§2.5 dispatch 形态（规格 P4）：直接吃 (effect, ctx)，不构造 Invocation。

        before/after_model 通知保留（④）；降级目标从 ctx.meta 的 typed RoutingTarget
        读（②降级守卫经 CallState 写入）；REFUSE 真拒答、未注册目标 TargetUnresolved
        折叠失败，绝不回退默认模型。
        """
        async def dispatch(effect: Effect, ctx: object) -> Result:  # purity-exempt: dispatch ctx 动态边界（存量，行号位移）
            target = self._extract_target(ctx)
            return await self._center(effect, ctx, target)
        return dispatch

    async def complete(
        self,
        request: LLMRequest,
        ctx: PortCtx,
        *,
        degrade_target: str | None = None,
    ) -> LLMResponse:
        """一次完整模型调用：缓存命中直接返回，否则路由→调用→校验→记账。

        参数：
            request: LLM 请求。ctx: Port 上下文（含 run_id/user_id）。
        返回：
            LLMResponse（缓存命中 / 正常返回 / 修复后返回）；strict 校验失败/修复超次时
            返回带 error="SCHEMA_INVALID: ..." 的响应（N3：不抛 ValueError 穿透成
            PORT_CRASHED；由 as_call 折叠为 Result.failure("SCHEMA_INVALID")）。
        """
        # 拒答优先于缓存：先过唯一决策点，非 LLMPort 三态直接折叠，绝不读缓存或调后端。
        resolution = self._resolve_llm(request, ctx, degrade_target=degrade_target)
        if not isinstance(resolution, LLMPort):
            return self._resolution_error_response(resolution)

        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:  # 命中即短路：不再调后端、不再记成本（usage 已随条目存好）
            return cached

        # P2a singleflight：相同 key 的并发 miss 共享一次完整调用（含记账/校验/缓存），
        # 后续等待者直接拿同一 Future 的结果，不再各调一次后端、各记一笔成本。
        key = cache_key_string(request, prompt_version=self._prompt_version)
        inflight = self._inflight.get(key)
        if inflight is None:  # 首个 miss 成为 owner 建 Future；并发后来者走 shield 等同一结果，不重复击穿
            loop = asyncio.get_running_loop()
            inflight = loop.create_future()
            # 消费 owner 路径 fut 的异常，避免无等待者时 "Future exception was never retrieved"；
            # 有等待者时不影响它们 await（exception() 只读取、不清除）。
            inflight.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )
            self._inflight[key] = inflight
            try:  # owner 独占后端调用；异常/取消必须广播给等待者，避免它们各自重试再次击穿
                response = await self._complete_miss(request, ctx, cast(LLMPortWithUsage, resolution))
                inflight.set_result(response)
            except BaseException as exc:
                # 异常/取消传播给所有等待者（避免它们各自重试再次击穿）。
                if not inflight.done():  # 幂等防御：已置终态时再 set 会抛 InvalidStateError 淹没原异常
                    inflight.set_exception(exc)
                raise
            finally:
                self._inflight.pop(key, None)
            return response
        return await asyncio.shield(inflight)

    async def _complete_miss(
        self, request: LLMRequest, ctx: PortCtx, llm: LLMPortWithUsage,
    ) -> LLMResponse:
        """一次未命中请求的完整链路：预留→调用→记账→校验→缓存（singleflight 内执行一次）。

        llm 由调用方 complete() 经唯一决策点解析后传入（避免重复路由）；
        P2b：调用前按预估成本 reserve（并发硬闸），成功后 commit 按实际结算，
        失败/异常 cancel 释放预留——彻底消除"先调用后扣费"的并发击穿。
        """
        reservation = None
        if self._cost_guard is not None:  # 有硬闸才预留；reserve 用预估兜底，失败即视为预算耗尽
            reservation = self._cost_guard.reserve(self._cost_estimator(request))
            if reservation is None:
                # 预算不足：调用前就拒，绝不发起后端请求。
                return LLMResponse(
                    content="", tool_calls=[], usage=Usage(),
                    error="COST_LIMIT: budget exhausted before call",
                )

        try:  # 包住整条调用链：任何异常/取消都必须走到下面释放预留，不留悬空额度
            first = await llm.complete(request, ctx)
            if first.error:  # 错误响应提前返回：不校验、不缓存；预留必须在此释放而非拖到结算
                # 后端失败：释放预留、照实记账（usage 为零由响应自带）。
                if reservation is not None and self._cost_guard is not None:
                    self._cost_guard.cancel(reservation)
                    reservation = None
                self._record_cost(first, ctx)
                return first

            # calls = 本次 miss 触发的全部后端调用响应（首次 + 每次修复）。
            # 每条调用各记一笔账、各计一份成本；绝不能用最终响应覆盖/重复记账。
            calls: list[LLMResponse] = [first]
            final = first
            if self._validator is not None:  # 仅装配 spec 时才有校验器；非结构化场景跳过校验直接结算
                # E6：按 spec.mode 决定是否修复；constrained/prompt 模式只校验不修复。
                if self._validator.should_repair:
                    calls, final = await self._complete_with_repair(first, request, ctx, llm=llm)
                elif self._spec is not None and self._spec.strict:  # 非修复模式靠 strict 兜底拒绝非法输出
                    errors = self._validator.check(first.content or "")
                    if errors:  # 有错即判非法：折叠成 error 响应，绝不流入缓存或工具调用
                        # N3：strict 失败折叠 error（不抛穿）；预留也要释放。
                        if reservation is not None and self._cost_guard is not None:
                            self._cost_guard.cancel(reservation)
                            reservation = None
                        return LLMResponse(
                            content="", tool_calls=[], usage=first.usage,
                            model=first.model, error=f"SCHEMA_INVALID: {errors}",
                        )

            # 预留按整条调用链的总成本一次结清；账本按每次调用逐条登记。
            self._settle_guard(sum(float(r.usage.cost_usd) for r in calls), reservation)
            reservation = None  # 已结清，避免 finally 重复 cancel
            for call in calls:
                self._ledger_record(call, ctx)

            # 非法输出绝不写缓存（否则污染后续请求）；仅合法响应入缓存。
            if not final.error:
                await self._cache.put(request, final, version=self._prompt_version)
            return final
        except BaseException:
            # 异常/取消：释放预留，不记支出。
            if reservation is not None and self._cost_guard is not None:
                self._cost_guard.cancel(reservation)
            raise

    async def stream(
        self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None
    ) -> AsyncIterator[str]:
        """流式补全（R4 §4.1）：缓存命中吐缓存，否则逐块转发并在结束时结算。

        参数：
            request: LLM 请求。ctx: Port 上下文。
        产出：
            文本块（逐块）；取消由消费方 aclose() 传播。
        说明：
            流式路径禁用结构化校验（无法校验半截流），不经过 R1 Effect 循环。
        """
        # 拒答优先于缓存：流路径先过唯一决策点；终止型三态抛稳定码错误，不吐缓存。
        resolution = self._resolve_llm(request, ctx, degrade_target=degrade_target)
        if not isinstance(resolution, LLMPort):
            self._raise_resolution_error(resolution)

        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:  # 命中直接吐缓存文本，跳过预留/后端/结算（usage 已随条目存好）
            # 流式是 Port 层，ctx 是 PortCtx（无 hook trace）；缓存命中信息随响应走。
            for ch in cached.content or "":
                yield ch
            return

        # P2b：流式也在调用前预留（硬闸一致）；流末按实际结算，断流/取消释放。
        reservation = None
        if self._cost_guard is not None:
            reservation = self._cost_guard.reserve(self._cost_estimator(request))
            if reservation is None:
                raise ValueError("cost budget exceeded (CostGuard) before stream")

        chunks: list[str] = []
        llm = resolution
        try:  # 断流/取消也从这里传播；只有正常流末才结算，异常一律释放预留
            # LLMPort.stream 返回 AsyncIterator（PEP 525 签名），直接 async for 迭代。
            async for chunk in llm.stream(request, ctx):
                chunks.append(chunk)
                yield chunk

            # 流结束：后端必须已把 usage 写入协议成员 last_usage（LLMPort 契约）。
            # 缺失即拒绝服务并显式报错——绝不记一笔假 zero-cost 掩盖账单缺口。
            usage = cast(LLMPortWithUsage, llm).last_usage
            if usage is None:
                raise ValueError(
                    "LLMPort.stream() finished without setting last_usage; "
                    "cost cannot be settled (fake/zero-cost accounting forbidden)"
                )
            full_response = LLMResponse(content="".join(chunks), usage=usage)
            self._record_cost(full_response, ctx, reservation=reservation)
            reservation = None
            await self._cache.put(request, full_response, version=self._prompt_version)
        except BaseException:  # 断流/取消：不结算不写缓存，取消预留以免额度悬空
            if reservation is not None and self._cost_guard is not None:
                self._cost_guard.cancel(reservation)
            raise

    def stream_events(
        self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """结构化流式事件：透传后端事件，遇 USAGE 结算成本。

        参数：
            request: LLM 请求。ctx: Port 上下文。
        产出：
            StreamEvent 序列（文本/工具/handoff/usage/done）；取消由 aclose() 传播。
        说明：
            若后端未发 USAGE 事件但 last_usage 已写（旧后端默认包装），在流末补结算。
        """
        return self._stream_events_impl(request, ctx, degrade_target=degrade_target)

    async def _stream_events_impl(
        self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """实现体：透传/包装后端事件，遇 USAGE 或流末 last_usage 结算，断流释放预留。"""
        # 拒答优先于缓存：事件流先过唯一决策点；终止型三态抛稳定码错误。
        resolution = self._resolve_llm(request, ctx, degrade_target=degrade_target)
        if not isinstance(resolution, LLMPort):
            self._raise_resolution_error(resolution)

        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=cached.content or "")
            yield StreamEvent(kind=StreamEventKind.USAGE, data=cached.usage)
            yield StreamEvent(kind=StreamEventKind.DONE)
            return

        # P2b：事件流同样调用前预留、流末结算（硬闸三路径一致）。
        reservation = None
        if self._cost_guard is not None:  # 与 complete/stream 同闸：预留失败即拒流，不建立事件源
            reservation = self._cost_guard.reserve(self._cost_estimator(request))
            if reservation is None:
                raise ValueError("cost budget exceeded (CostGuard) before stream_events")

        chunks: list[str] = []
        settled: Usage | None = None
        llm = resolution
        try:  # 同 stream：仅正常流末结算；异常/取消取消预留，绝不留半途账单
            # 后端有 stream_events 直接用；旧后端用帮助函数把文本流包成事件流。
            # 规则 6 例外：stream_events 是可选能力探测（具名固定字符串），不在 LLMPort 强制协议内。
            if has_stream_events(llm):  # 能力探测决定事件源：有则用原生事件，旧后端走文本包装兜底
                stream_events_fn = getattr(llm, "stream_events")  # noqa: B009
                source = stream_events_fn(request, ctx)
            else:
                source = wrap_text_stream(llm, request, ctx)
            async for event in source:  # 透传同时旁路累积文本与 usage，供流末兜底结算/写缓存
                if event.kind is StreamEventKind.TEXT_DELTA and isinstance(event.data, str):  # 只收文本块拼完整响应；守卫挡住畸形 data
                    chunks.append(event.data)
                elif event.kind is StreamEventKind.USAGE and isinstance(event.data, Usage):  # USAGE 优先作结算依据；守卫过滤同 kind 的坏负载
                    settled = event.data
                yield event  # 原样透传，包含工具/handoff/done

            # 结算：优先事件里的 USAGE，其次后端 last_usage；两者都无则拒绝（不记假成本）。
            usage = settled or cast(LLMPortWithUsage, llm).last_usage  # 断流可能收不到 USAGE 事件，靠 last_usage 兜底，绝不记假零成本
            if usage is None:
                raise ValueError(
                    "LLMPort.stream_events() finished without USAGE or last_usage; "
                    "cost cannot be settled (fake/zero-cost accounting forbidden)"
                )
            full = LLMResponse(content="".join(chunks), usage=usage)
            self._record_cost(full, ctx, reservation=reservation)
            reservation = None
            await self._cache.put(request, full, version=self._prompt_version)
        except BaseException:  # 断流/取消：释放预留；不结算不写缓存，避免半途账单与污染条目
            if reservation is not None and self._cost_guard is not None:
                self._cost_guard.cancel(reservation)
            raise

    def _settle_guard(self, cost: float, reservation: Reservation | None) -> None:
        """按实际成本结清成本硬闸（一次调用链只结清一次）。"""
        guard = self._cost_guard
        if reservation is not None and guard is not None:  # 有预留走 commit 按实结清；差额退还/补扣
            guard.commit(reservation, cost)
        elif guard is not None:
            # 无预留路径：旧 allow/charge 事后语义。
            if not guard.allow(cost):
                raise ValueError("cost budget exceeded (CostGuard)")
            guard.charge(cost)

    def _ledger_record(self, response: LLMResponse, ctx: PortCtx) -> None:
        """把一次后端调用的 usage 登记进账本（与成本硬闸结算解耦）。"""
        view = coerce_port_ctx(ctx)  # 唯一 PortCtx 动态边界（规则 6）
        self.ledger.record(CostRecord(
            run_id=ctx.run_id,
            agent_id=view.agent_id,
            user_id=view.user_id,
            model=response.model,
            usage=response.usage,
        ))

    def _record_cost(
        self, response: LLMResponse, ctx: PortCtx, *, reservation: Reservation | None = None,
    ) -> None:
        """单次响应的便利入口：先结清硬闸，再记一条账。

        多次调用链（repair）由 _complete_miss 分别调 _settle_guard/_ledger_record，
        避免最终响应被重复记账或首次响应丢失。
        """
        self._settle_guard(float(response.usage.cost_usd), reservation)
        self._ledger_record(response, ctx)

    async def _complete_with_repair(
        self,
        response: LLMResponse,
        request: LLMRequest,
        ctx: PortCtx,
        *,
        llm: LLMPortWithUsage | None = None,
    ) -> tuple[list[LLMResponse], LLMResponse]:
        """异步修复循环：最多 max_repair 次，每次把校验错误附给模型。

        返回 (全部后端调用响应, 最终响应)：调用方对每条响应各记一次账，
        修复循环本身不记账（避免最终响应被重复记账、首次响应丢失）。

        llm: 本次调用已解析的后端（缺省用默认，兼容直接调用）。
        """
        assert self._validator is not None
        assert self._spec is not None  # validator 存在时 spec 必存在（构造时同源创建）
        spec = self._spec
        current = response
        calls: list[LLMResponse] = [response]
        for _ in range(self._validator.max_repair + 1):  # +1 是首次校验，其后最多 max_repair 次修复，防止无限重试烧成本
            # 走 validator 持有的校验器（默认或注入），保证"校验"与"修复"同一口径。
            errors = self._validator.check(current.content or "")
            if not errors:  # 一旦合法立即返回，避免对已合规输出再发起一次无谓修复调用
                return calls, current
            repair_prompt = self._validator.build_repair_prompt(current.content or "", errors)
            # 统一复用 R1 Message 四字段具名结构（规则 4）：消除仅两字段的重复 ChatMessage。
            repair_message = Message(role="system", content=repair_prompt)
            patched = LLMRequest(
                messages=[*request.messages, repair_message.to_dict()],
                model=request.model,
            )
            current = await (llm or self._llm).complete(patched, ctx)
            calls.append(current)
        # 超次：strict 模式拒答（N3：折叠为 SCHEMA_INVALID error 响应，不抛穿成 PORT_CRASHED）。
        if spec.strict:
            return calls, LLMResponse(
                content="", tool_calls=[], usage=current.usage,
                model=current.model, error="SCHEMA_INVALID: repair exhausted",
            )
        return calls, current
