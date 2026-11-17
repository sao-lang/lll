"""R4 模型网关：编排 缓存→路由→调用→结构化校验/修复→成本归因。

上层唯一入口 ModelGateway.complete()；换 LLMPort 实现时上层零改动（R4 §8 验收 3）。
成本归因只记录 LLMPort 响应里的 usage，不自行估算；CostGuard 保证不绕过预算。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from harness._ctxview import coerce_port_ctx
from hooks import (
    LLMPort,
    LLMRequest,
    LLMResponse,
    PortCtx,
    StreamEvent,
    StreamEventKind,
    Usage,
)
from runtime import Message

from .cache import CachePort, ExactCache
from .router import ModelRouter, RuleRouter
from .stream import has_stream_events, wrap_text_stream
from .structured import StructuredOutputValidator, validate_json_schema
from .types import (
    CostAttributor,
    CostGuard,
    CostRecord,
    StructuredOutputSpec,
)

__all__ = ["ModelGateway"]


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
        prompt_version: str = "v1",
        cost_guard: CostGuard | None = None,
    ) -> None:
        """装配网关：只 llm 必填，其余缺省用默认实现。

        参数：
            llm: 模型后端（实现 LLMPort.complete/stream）。
            router: 路由策略（缺省规则路由）。cache: 缓存（缺省精确缓存）。
            spec: 结构化输出规格（None 表示不校验）。
            prompt_version: prompt 版本（进缓存键）。cost_guard: 成本硬闸。
        """
        self._llm = llm
        self._router = router or RuleRouter()
        self._cache = cache if cache is not None else ExactCache()
        self._spec = spec
        self._validator = StructuredOutputValidator(spec) if spec else None
        self._prompt_version = prompt_version
        self._cost_guard = cost_guard
        self.ledger = CostAttributor()
        self.cache = self._cache

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        """一次完整模型调用：缓存命中直接返回，否则路由→调用→校验→记账。

        参数：
            request: LLM 请求。ctx: Port 上下文（含 run_id/user_id）。
        返回：
            LLMResponse（缓存命中 / 正常返回 / 修复后返回）。
        异常：
            ValueError: 结构化校验超次且 strict（拒答）。
        """
        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:
            return cached

        response = await self._llm.complete(request, ctx)
        self._record_cost(response, ctx)

        if self._validator is not None:
            response = await self._complete_with_repair(response, request, ctx)

        await self._cache.put(request, response, version=self._prompt_version)
        return response

    async def stream(self, request: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
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
        # LLMPort.stream 返回 AsyncIterator（PEP 525 签名），直接 async for 迭代。
        async for chunk in self._llm.stream(request, ctx):
            chunks.append(chunk)
            yield chunk

        # 流结束：后端必须已把 usage 写入协议成员 last_usage（LLMPort 契约）。
        # 缺失即拒绝服务并显式报错——绝不记一笔假 zero-cost 掩盖账单缺口。
        usage = self._llm.last_usage
        if usage is None:
            raise ValueError(
                "LLMPort.stream() finished without setting last_usage; "
                "cost cannot be settled (fake/zero-cost accounting forbidden)"
            )
        full_response = LLMResponse(content="".join(chunks), usage=usage)
        self._record_cost(full_response, ctx)
        await self._cache.put(request, full_response, version=self._prompt_version)

    def stream_events(
        self, request: LLMRequest, ctx: PortCtx,
    ) -> AsyncIterator[StreamEvent]:
        """结构化流式事件（R4 §4.1 v1.2）：透传后端事件，遇 USAGE 结算成本。

        参数：
            request: LLM 请求。ctx: Port 上下文。
        产出：
            StreamEvent 序列（文本/工具/handoff/usage/done）；取消由 aclose() 传播。
        说明：
            若后端未发 USAGE 事件但 last_usage 已写（旧后端默认包装），在流末补结算。
        """
        return self._stream_events_impl(request, ctx)

    async def _stream_events_impl(
        self, request: LLMRequest, ctx: PortCtx,
    ) -> AsyncIterator[StreamEvent]:
        cached = await self._cache.get(request, version=self._prompt_version)
        if cached is not None:
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=cached.content or "")
            yield StreamEvent(kind=StreamEventKind.USAGE, data=cached.usage)
            yield StreamEvent(kind=StreamEventKind.DONE)
            return

        chunks: list[str] = []
        settled: Usage | None = None
        # 后端有 stream_events 直接用；旧后端用帮助函数把文本流包成事件流。
        # 规则 8 例外：stream_events 是可选能力探测（具名固定字符串），不在 LLMPort 强制协议内。
        if has_stream_events(self._llm):
            stream_events_fn = getattr(self._llm, "stream_events")
            source = stream_events_fn(request, ctx)
        else:
            source = wrap_text_stream(self._llm, request, ctx)
        async for event in source:
            if event.kind is StreamEventKind.TEXT_DELTA and isinstance(event.data, str):
                chunks.append(event.data)
            elif event.kind is StreamEventKind.USAGE and isinstance(event.data, Usage):
                settled = event.data
            yield event  # 原样透传，包含工具/handoff/done

        # 结算：优先事件里的 USAGE，其次后端 last_usage；两者都无则拒绝（不记假成本）。
        usage = settled or self._llm.last_usage
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
                agent_id/user_id 由 coerce_port_ctx 具名视图提供（规则 8）。
        """
        cost = float(response.usage.cost_usd)
        if self._cost_guard is not None and not self._cost_guard.allow(cost):
            raise ValueError("cost budget exceeded (CostGuard)")
        if self._cost_guard is not None:
            self._cost_guard.charge(cost)
        view = coerce_port_ctx(ctx)  # 唯一 PortCtx 动态边界（规则 8）
        self.ledger.record(CostRecord(
            run_id=ctx.run_id,
            agent_id=view.agent_id,
            user_id=view.user_id,
            model=response.model,
            usage=response.usage,
        ))

    async def _complete_with_repair(
        self, response: LLMResponse, request: LLMRequest, ctx: PortCtx
    ) -> LLMResponse:
        """异步修复循环：最多 max_repair 次，每次把校验错误附给模型。"""
        assert self._validator is not None
        assert self._spec is not None  # validator 存在时 spec 必存在（构造时同源创建）
        spec = self._spec
        current = response
        for _ in range(self._validator.max_repair + 1):
            errors = validate_json_schema(current.content or "", spec.json_schema)
            if not errors:
                return current
            repair_prompt = self._validator.build_repair_prompt(current.content or "", errors)
            # 统一复用 R1 Message 四字段具名结构（规则 3）：消除仅两字段的重复 ChatMessage。
            repair_message = Message(role="system", content=repair_prompt)
            patched = LLMRequest(
                messages=[*request.messages, repair_message.to_dict()],
                model=request.model,
            )
            current = await self._llm.complete(patched, ctx)
            self._record_cost(current, ctx)
        # 超次：strict 模式拒答（抛错，绝不把非法输出当合法返回）。
        if spec.strict:
            raise ValueError("structured output repair exhausted")
        return current
