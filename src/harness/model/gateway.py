"""R4 模型网关：编排 缓存→路由→调用→结构化校验/修复→成本归因。

上层唯一入口 ModelGateway.complete()；换 LLMPort 实现时上层零改动（R4 §8 验收 3）。
成本归因只记录 LLMPort 响应里的 usage，不自行估算；CostGuard 保证不绕过预算。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from harness._ctxview import as_port_ctx, coerce_port_ctx
from hooks import (
    LLMPort,
    LLMRequest,
    LLMResponse,
    PortCtx,
    StreamEvent,
    StreamEventKind,
    Usage,
)
from runtime import Effect, EffectKind, Message, Result

from .cache import CachePort, ExactCache
from .registry import ModelRegistry
from .router import NO_DEFAULT, ModelRouter, RuleRouter
from .stream import has_stream_events, wrap_text_stream
from .structured import SchemaValidator, StructuredOutputValidator
from .types import (
    CostAttributor,
    CostGuard,
    CostRecord,
    Reservation,
    StructuredOutputSpec,
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
        llm: LLMPort,
        router: ModelRouter | None = None,
        cache: CachePort | None = None,
        spec: StructuredOutputSpec | None = None,
        validator: SchemaValidator | None = None,
        prompt_version: str = "v1",
        cost_guard: CostGuard | None = None,
        registry: ModelRegistry | None = None,
        cost_estimator: Callable[[LLMRequest], float] | None = None,
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
        self.ledger = CostAttributor()
        self.cache = self._cache
        # P2a singleflight：同一缓存 key 的并发 miss 共享一次后端调用，
        # 避免击穿（40 并发 → 真调 40 次）、重复记账。
        self._inflight: dict[str, asyncio.Future[LLMResponse]] = {}

    def _resolve_llm(
        self, request: LLMRequest, ctx: PortCtx, *, degrade_target: str | None = None
    ) -> LLMPort:
        """解析本次调用的真实后端：router 选模型 → registry 映射实例（v1.5 接通）。

        参数：
            request: LLM 请求。ctx: Port 上下文。
            degrade_target: 降级目标名（优先于 router 结果；来自 dispatch 回传）。
        返回：
            目标 LLMPort 实例；未注册/无路由目标时回退默认 llm。
        说明：
            - router 总是被调用（修复 _router 空转），路由理由进 ctx.meta 供 trace。
            - 注册表缺省（None）时 router 仍调用但不切实例——兼容旧行为。
        """
        # 构造 router 需要的 Effect：task_hint 来自请求（E4 修复，原来恒空导致路由空转）
        effect = Effect(
            kind=EffectKind.LLM,
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
        if target is None or self._registry is None:
            # 无目标或未接注册表时无法映射实例，回退默认后端——兼容未装配注册表的旧行为。
            return self._llm
        resolved = self._registry.resolve_llm(target)
        if resolved is None:
            # 降级目标必须可解析；静默回退默认模型会让熔断/降级形同虚设（ARCH-v5 红线）。
            raise ValueError(f"degrade target not registered: {target!r}")
        return resolved

    def as_call(self) -> Callable[[Effect, object], object]:
        """给 R1 Runner 的 call 适配器：Effect→LLMRequest→complete→Result。

        ARCH-v5 降级接力：从 ctx.degrade_target（typed 具名桥接字段，Host 写入）读目标，
        REFUSE → 真拒答 Result.failure("REFUSED")，绝不调后端、绝不回退默认模型。
        """
        async def adapter(effect: Effect, ctx: object) -> Result:
            """R1 call 适配器：读降级目标 → 构造请求 → complete → 折叠 error 为 Result。"""
            # 规则 6 例外：degrade_target 是 RuntimeCtx 的声明字段（具名桥接，非裸 meta key），
            # 鸭子读取兼容测试用简单 ctx。
            target = getattr(ctx, "degrade_target", None)

            if target == REFUSE_TARGET:
                # 拒答优先于一切：不解析、不缓存、不调用后端。
                return Result.failure("REFUSED", "degraded to refusal; no backend invoked")

            payload = effect.payload
            request = LLMRequest(
                messages=list(payload.get("messages", [])),
                model=str(payload.get("model") or "") or None,
                task_hint=str(payload.get("task_hint") or "") or None,
            )
            port_ctx = as_port_ctx(ctx)
            response = await self.complete(request, port_ctx, degrade_target=target)
            # 后端错误（E3）：折叠为失败 Result，让 R3 的重试/熔断/降级能看到。
            if response.error:
                return Result.failure(response.error.split(":")[0], response.error)
            return Result.success({
                "content": response.content,
                "tool_calls": response.tool_calls,
                "usage": response.usage,
            })

        return adapter

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
        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:  # 命中即短路：不再路由、不再调后端、不再记成本（usage 已随条目存好）
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
                response = await self._complete_miss(request, ctx, degrade_target)
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
        self, request: LLMRequest, ctx: PortCtx, degrade_target: str | None,
    ) -> LLMResponse:
        """一次未命中请求的完整链路：预留→路由→调用→记账→校验→缓存（singleflight 内执行一次）。

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
            llm = self._resolve_llm(request, ctx, degrade_target=degrade_target)
            response = await llm.complete(request, ctx)
            if response.error:  # 错误响应提前返回：不校验、不缓存；预留必须在此释放而非拖到结算
                # 后端失败：释放预留、照实记账（usage 为零由响应自带）。
                if reservation is not None and self._cost_guard is not None:
                    self._cost_guard.cancel(reservation)
                    reservation = None
                self._record_cost(response, ctx)
                return response

            if self._validator is not None:  # 仅装配 spec 时才有校验器；非结构化场景跳过校验直接结算
                # E6：按 spec.mode 决定是否修复；constrained/prompt 模式只校验不修复。
                if self._validator.should_repair:
                    response = await self._complete_with_repair(response, request, ctx, llm=llm)
                elif self._spec is not None and self._spec.strict:  # 非修复模式靠 strict 兜底拒绝非法输出
                    errors = self._validator.check(response.content or "")
                    if errors:  # 有错即判非法：折叠成 error 响应，绝不流入缓存或工具调用
                        # N3：strict 失败折叠 error（不抛穿）；预留也要释放。
                        if reservation is not None and self._cost_guard is not None:
                            self._cost_guard.cancel(reservation)
                            reservation = None
                        return LLMResponse(
                            content="", tool_calls=[], usage=response.usage,
                            model=response.model, error=f"SCHEMA_INVALID: {errors}",
                        )

            # 成功后按实际成本结算预留并记账（reservation 交给 _record_cost commit）。
            self._record_cost(response, ctx, reservation=reservation)
            reservation = None  # 已结清，避免 finally 重复 cancel

            # 非法输出绝不写缓存（否则污染后续请求）；仅合法响应入缓存。
            if not response.error:
                await self._cache.put(request, response, version=self._prompt_version)
            return response
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
        llm = self._resolve_llm(request, ctx, degrade_target=degrade_target)
        try:  # 断流/取消也从这里传播；只有正常流末才结算，异常一律释放预留
            # LLMPort.stream 返回 AsyncIterator（PEP 525 签名），直接 async for 迭代。
            async for chunk in llm.stream(request, ctx):
                chunks.append(chunk)
                yield chunk

            # 流结束：后端必须已把 usage 写入协议成员 last_usage（LLMPort 契约）。
            # 缺失即拒绝服务并显式报错——绝不记一笔假 zero-cost 掩盖账单缺口。
            usage = llm.last_usage
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
        """结构化流式事件（R4 §4.1 v1.2）：透传后端事件，遇 USAGE 结算成本。

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
        llm = self._resolve_llm(request, ctx, degrade_target=degrade_target)
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
            usage = settled or llm.last_usage  # 断流可能收不到 USAGE 事件，靠 last_usage 兜底，绝不记假零成本
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

    def _record_cost(
        self, response: LLMResponse, ctx: PortCtx, *, reservation: Reservation | None = None,
    ) -> None:
        """从响应的 usage 记录成本（不自行估算），结清成本硬闸。

        参数：
            response: LLM 响应（usage 提供 token/成本）。ctx: Port 上下文，
                agent_id/user_id 由 coerce_port_ctx 具名视图提供（规则 6）。
            reservation: 调用前的预留凭证（P2b）；有则 commit 按实际结算，
                无则走旧 allow/charge 兼容路径（流式等未预留的场景）。
        """
        cost = float(response.usage.cost_usd)
        if reservation is not None and self._cost_guard is not None:  # 有预留走 commit 按实结清；差额退还/补扣，不按预估死扣
            # P2b：按实际成本结清预留（差额退还/补扣）。
            self._cost_guard.commit(reservation, cost)
        elif self._cost_guard is not None:
            # 兼容路径（无预留）：旧 allow/charge 事后语义。
            if not self._cost_guard.allow(cost):
                raise ValueError("cost budget exceeded (CostGuard)")
            self._cost_guard.charge(cost)
        view = coerce_port_ctx(ctx)  # 唯一 PortCtx 动态边界（规则 6）
        self.ledger.record(CostRecord(
            run_id=ctx.run_id,
            agent_id=view.agent_id,
            user_id=view.user_id,
            model=response.model,
            usage=response.usage,
        ))

    async def _complete_with_repair(
        self,
        response: LLMResponse,
        request: LLMRequest,
        ctx: PortCtx,
        *,
        llm: LLMPort | None = None,
    ) -> LLMResponse:
        """异步修复循环：最多 max_repair 次，每次把校验错误附给模型。

        llm: 本次调用已解析的后端（缺省用默认，兼容直接调用）。
        """
        assert self._validator is not None
        assert self._spec is not None  # validator 存在时 spec 必存在（构造时同源创建）
        spec = self._spec
        current = response
        for _ in range(self._validator.max_repair + 1):  # +1 是首次校验，其后最多 max_repair 次修复，防止无限重试烧成本
            # 走 validator 持有的校验器（默认或注入），保证"校验"与"修复"同一口径。
            errors = self._validator.check(current.content or "")
            if not errors:  # 一旦合法立即返回，避免对已合规输出再发起一次无谓修复调用
                return current
            repair_prompt = self._validator.build_repair_prompt(current.content or "", errors)
            # 统一复用 R1 Message 四字段具名结构（规则 4）：消除仅两字段的重复 ChatMessage。
            repair_message = Message(role="system", content=repair_prompt)
            patched = LLMRequest(
                messages=[*request.messages, repair_message.to_dict()],
                model=request.model,
            )
            current = await (llm or self._llm).complete(patched, ctx)
            self._record_cost(current, ctx)
        # 超次：strict 模式拒答（N3：折叠为 SCHEMA_INVALID error 响应，不抛穿成 PORT_CRASHED）。
        if spec.strict:
            return LLMResponse(
                content="", tool_calls=[], usage=current.usage,
                model=current.model, error="SCHEMA_INVALID: repair exhausted",
            )
        return current
