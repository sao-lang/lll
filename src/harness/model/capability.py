"""R4 能力装配：ModelCapability 构造网关并持有共用 ModelRegistry。

降级目标后端通过 add_backend 注册到同一 ModelRegistry，供网关按 typed 目标切换；
容器/网关/能力共用这份注册表（P6 两表统一，载体换为共用 Registry）。
"""
from __future__ import annotations

from aop import AOPContainer
from hooks import LifecycleBus, LLMPort, Registry

from .cache import ExactCache, CachePort
from .gateway import ModelGateway
from .registry import InMemoryModelRegistry, ModelRegistry
from .router import RuleRouter
from .structured import SchemaValidator
from .types import CostGuard, StructuredOutputSpec

__all__ = ["ModelCapability"]


class ModelCapability:
    """模型能力包：把 llm/router/cache 注册进共用 Provider 注册表。

    属性：
        name/version/requires: 能力级元数据（R13 Capability 契约）。
    """
    name = "model"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        llm: LLMPort,
        router: RuleRouter | None = None,
        cache: CachePort | None = None,
        spec: StructuredOutputSpec | None = None,
        validator: SchemaValidator | None = None,
        registry: ModelRegistry | None = None,
        bus: LifecycleBus | None = None,
        cost_guard: CostGuard | None = None,
    ) -> None:
        """装配能力：llm 必填，其余策略可替换。

        参数：
            cost_guard: 成本硬闸；透传给 gateway 使生产链路可启用（默认 None 不限制）。
        """
        self._llm = llm
        self._router = router
        self._cache = cache
        self._spec = spec
        self._validator = validator
        self._bus = bus
        self._cost_guard = cost_guard
        # 默认建 ModelRegistry 并注册 "primary"，让降级目标可解析。
        self._model_registry = registry or InMemoryModelRegistry()
        self._model_registry.register_llm("primary", llm)

    def install(self, container: AOPContainer | None = None, reg: Registry | None = None) -> Registry:
        """统一装配：把 llm/router/cache 注册进共用 Provider 注册表。

        参数：
            container: 可选；若给定，其 registry 指向同一份注册表（两表统一）。
            reg: 注入的注册表；缺省用本能力持有的实例。
        返回：
            共用注册表（支持链式）。
        """
        if reg is not None:
            self._model_registry = reg  # type: ignore[assignment]
        registry = self._model_registry
        registry.register("llm", self._llm)
        registry.register("model_router", self._router or RuleRouter())
        registry.register("model_cache", self._cache or ExactCache())
        if container is not None:
            container.registry = registry
        return registry  # type: ignore[return-value]

    @property
    def llm(self) -> LLMPort:
        """默认后端实例（装配核对用）。"""
        return self._llm

    @property
    def registry(self) -> ModelRegistry:
        """本能力持有的注册表（装配核对用）。"""
        return self._model_registry

    def gateway(self) -> ModelGateway:
        """构造模型网关（传入共用 registry 与通知总线）。"""
        return ModelGateway(
            llm=self._llm, router=self._router, cache=self._cache,
            spec=self._spec, validator=self._validator,
            registry=self._model_registry, bus=self._bus,
            cost_guard=self._cost_guard,
        )

    def container(self) -> AOPContainer:
        """构造与网关共用同一 Registry 的 AOP 容器（P6 两表统一的标准装配入口）。

        返回：
            已注入共用 registry 的容器；core 为网关 as_call。
        """
        return AOPContainer(self.gateway().as_call(), registry=self._model_registry, bus=self._bus)

    def add_backend(self, name: str, llm: LLMPort) -> None:
        """注册降级目标后端（如 "backup"），供网关按 typed 目标切换。"""
        self._model_registry.register_llm(name, llm)
