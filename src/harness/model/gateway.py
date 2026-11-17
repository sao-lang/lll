"""R4 模型网关：编排 缓存→路由→调用→结构化校验/修复→成本归因。

上层唯一入口 ModelGateway.complete()；换 LLMPort 实现时上层零改动（R4 §8 验收 3）。
成本归因只记录 LLMPort 响应里的 usage，不自行估算；CostGuard 保证不绕过预算。
"""
from __future__ import annotations

from harness._ctxview import coerce_port_ctx
from hooks import LLMPort, LLMRequest, LLMResponse, PortCtx

from .cache import ExactCache, SemanticCache
from .router import ModelRouter, RuleRouter
from .structured import StructuredOutputValidator, validate_json_schema
from .types import (
    ChatMessage,
    CostAttributor,
    CostGuard,
    CostRecord,
    StructuredOutputSpec,
    build_cache_key,
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
        cache: SemanticCache | None = None,
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
        key = build_cache_key(request, prompt_version=self._prompt_version).value
        cached = self._cache.get(key, version=self._prompt_version)
        if cached is not None:
            return cached

        response = await self._llm.complete(request, ctx)
        self._record_cost(response, ctx)

        if self._validator is not None:
            response = await self._complete_with_repair(response, request, ctx)

        self._cache.put(key, response, version=self._prompt_version)
        return response

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
            # 具名消息结构（规则 3）：明确字段 role/content 不写动态 dict 拼装。
            repair_message = ChatMessage(role="system", content=repair_prompt)
            patched = LLMRequest(
                messages=[*request.messages, dict(repair_message)],
                model=request.model,
            )
            current = await self._llm.complete(patched, ctx)
            self._record_cost(current, ctx)
        # 超次：strict 模式拒答（抛错，绝不把非法输出当合法返回）。
        if spec.strict:
            raise ValueError("structured output repair exhausted")
        return current
