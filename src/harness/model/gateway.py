"""R4 模型网关：编排 缓存→路由→调用→结构化校验/修复→成本归因。

上层唯一入口 ModelGateway.complete()；换 LLMPort 实现时上层零改动（R4 §8 验收 3）。
成本归因只记录 LLMPort 响应里的 usage，不自行估算；CostGuard 保证不绕过预算。
"""
from __future__ import annotations

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
    StructuredOutputSpec,
)

__all__ = ["ModelGateway"]

# R4 本地拒绝目标常量（规则 5）：值与 R3 DegradeMode.REFUSE 对齐，
# 但网关不依赖 R3，故在本层用具名常量替代魔法串，比较时右侧是常量名而非字符串字面量。
REFUSE_TARGET = "refuse"


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
        """
        self._llm = llm
        self._router = router or RuleRouter()
        self._cache = cache if cache is not None else ExactCache()
        self._registry = registry
        self._spec = spec
        self._validator = StructuredOutputValidator(spec, validator=validator) if spec else None
        self._prompt_version = prompt_version
        self._cost_guard = cost_guard
        self.ledger = CostAttributor()
        self.cache = self._cache

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
        if cached is not None:
            return cached

        llm = self._resolve_llm(request, ctx, degrade_target=degrade_target)
        response = await llm.complete(request, ctx)
        if response.error:
            return response  # 失败响应不校验、不缓存、照实记账（usage 为零）
        self._record_cost(response, ctx)

        if self._validator is not None:
            # E6：按 spec.mode 决定是否修复；constrained/prompt 模式只校验不修复。
            if self._validator.should_repair:
                response = await self._complete_with_repair(response, request, ctx, llm=llm)
            elif self._spec is not None and self._spec.strict:
                errors = self._validator.check(response.content or "")
                if errors:
                    # N3：strict 校验失败折叠为带 error 的响应（不抛 ValueError 穿透成
                    # PORT_CRASHED）；as_call 读 error 前缀得到 SCHEMA_INVALID 语义。
                    return LLMResponse(
                        content="", tool_calls=[], usage=response.usage,
                        model=response.model, error=f"SCHEMA_INVALID: {errors}",
                    )

        # 非法输出绝不写缓存（否则污染后续请求）；仅合法响应入缓存。
        if not response.error:
            await self._cache.put(request, response, version=self._prompt_version)
        return response

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
        if cached is not None:
            # 流式是 Port 层，ctx 是 PortCtx（无 hook trace）；缓存命中信息随响应走。
            for ch in cached.content or "":
                yield ch
            return

        chunks: list[str] = []
        llm = self._resolve_llm(request, ctx, degrade_target=degrade_target)
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
        self._record_cost(full_response, ctx)
        await self._cache.put(request, full_response, version=self._prompt_version)

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
        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=cached.content or "")
            yield StreamEvent(kind=StreamEventKind.USAGE, data=cached.usage)
            yield StreamEvent(kind=StreamEventKind.DONE)
            return

        chunks: list[str] = []
        settled: Usage | None = None
        llm = self._resolve_llm(request, ctx, degrade_target=degrade_target)
        # 后端有 stream_events 直接用；旧后端用帮助函数把文本流包成事件流。
        # 规则 6 例外：stream_events 是可选能力探测（具名固定字符串），不在 LLMPort 强制协议内。
        if has_stream_events(llm):
            stream_events_fn = getattr(llm, "stream_events")  # noqa: B009
            source = stream_events_fn(request, ctx)
        else:
            source = wrap_text_stream(llm, request, ctx)
        async for event in source:
            if event.kind is StreamEventKind.TEXT_DELTA and isinstance(event.data, str):
                chunks.append(event.data)
            elif event.kind is StreamEventKind.USAGE and isinstance(event.data, Usage):
                settled = event.data
            yield event  # 原样透传，包含工具/handoff/done

        # 结算：优先事件里的 USAGE，其次后端 last_usage；两者都无则拒绝（不记假成本）。
        usage = settled or llm.last_usage
        if usage is None:
            raise ValueError(
                "LLMPort.stream_events() finished without USAGE or last_usage; "
                "cost cannot be settled (fake/zero-cost accounting forbidden)"
            )
        full = LLMResponse(content="".join(chunks), usage=usage)
        self._record_cost(full, ctx)
        await self._cache.put(request, full, version=self._prompt_version)

    def _record_cost(self, response: LLMResponse, ctx: PortCtx) -> None:
        """从响应的 usage 记录成本（不自行估算），过成本硬闸。

        参数：
            response: LLM 响应（usage 提供 token/成本）。ctx: Port 上下文，
                agent_id/user_id 由 coerce_port_ctx 具名视图提供（规则 6）。
        """
        cost = float(response.usage.cost_usd)
        if self._cost_guard is not None and not self._cost_guard.allow(cost):
            raise ValueError("cost budget exceeded (CostGuard)")
        if self._cost_guard is not None:
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
        for _ in range(self._validator.max_repair + 1):
            # 走 validator 持有的校验器（默认或注入），保证"校验"与"修复"同一口径。
            errors = self._validator.check(current.content or "")
            if not errors:
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
