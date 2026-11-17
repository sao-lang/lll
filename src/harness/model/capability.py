"""R4 能力装配：ModelCapability 把 LLM 后端/路由/缓存注册到 HookBus.registry。

三层插拔模型（R13）的能力级开关：装了经 ModelGateway 生效，不装缺席。
网关本身不在 bus 上挂 hook（模型调用由 Effect(LLM) 经 R1→LLMPort 触发），
本能力负责把可替换策略注册进 Registry，供装配层解析替换。
"""
from __future__ import annotations

from hooks import HookBus, LLMPort, Registry

from .cache import ExactCache
from .gateway import ModelGateway
from .router import RuleRouter
from .types import StructuredOutputSpec

__all__ = ["ModelCapability"]


class ModelCapability:
    """模型能力包：把 llm/router/cache 注册进 Provider 注册表。

    属性：
        name/version/requires: 能力级元数据（R13 Capability 契约）。
    """

    name = "model"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        llm: LLMPort,
        router: RuleRouter | None = None,
        cache: ExactCache | None = None,
        spec: StructuredOutputSpec | None = None,
    ) -> None:
        """装配能力：llm 必填，其余策略可替换。"""
        self._llm = llm
        self._router = router
        self._cache = cache
        self._spec = spec

    def gateway(self) -> ModelGateway:
        """构造模型网关（上层调用入口）。"""
        return ModelGateway(
            llm=self._llm, router=self._router, cache=self._cache, spec=self._spec
        )

    def install(self, bus: HookBus, reg: Registry | None = None) -> HookBus:
        """把 llm/router/cache 注册到总线的 Provider 注册表。

        参数：
            bus: 目标 HookBus。reg: 目标注册表（缺省用 bus.registry）。
        返回：
            同一 bus（链式调用）。
        """
        registry = reg or bus.registry
        registry.register("llm", self._llm)  # LLMPort 实现带 name，满足 Provider 契约
        registry.register("model_router", self._router or RuleRouter())
        registry.register("model_cache", self._cache or ExactCache())
        return bus
