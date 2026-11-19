"""R4 能力装配（ARCH-v5）：ModelCapability 构造网关并持有 ModelRegistry。

降级目标后端通过 add_backend 注册到同一 ModelRegistry，供网关按 target 切换。
"""
from __future__ import annotations

from hooks import Host, LLMPort

from .cache import ExactCache, CachePort
from .gateway import ModelGateway
from .registry import InMemoryModelRegistry, ModelRegistry
from .router import RuleRouter
from .structured import SchemaValidator
from .types import StructuredOutputSpec

__all__ = ["ModelCapability"]


class ModelCapability:
    """模型能力包：把 llm/router/cache 注册进 Provider 注册表。

    属性：
        name/version/requires: 能力级元数据（R13 Capability 契约）。
    """

    name = "model"
    version = "5.0.0"
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
    ) -> None:
        """装配能力：llm 必填，其余策略可替换。"""
        self._llm = llm
        self._router = router
        self._cache = cache
        self._spec = spec
        self._validator = validator
        # ARCH-v5 修复：默认建 ModelRegistry 并注册 "primary"，让降级目标可解析。
        self._model_registry = registry or InMemoryModelRegistry()
        self._model_registry.register_llm("primary", llm)

    def install(self, host: Host, reg: ModelRegistry | None = None) -> Host:
        """R4 §5.6 统一装配：把 llm/router/cache 注册进统一注册表并令两表同源。

        参数：
            host: R2 Host；其 registry 指向同一实例（供 hook 总线与网关共用）。
            reg: 注入的注册表；缺省用本能力持有的实例（无则新建）。
        返回：
            host（支持链式装配）。
        """
        if reg is not None:
            # 注入注册表时，本能力后续 gateway 复用同一实例（两表统一）。
            self._model_registry = reg
        registry = self._model_registry
        # 具名 provider 三项注册（同名同实例幂等，异实例拒绝由注册表保证）。
        registry.register("llm", self._llm)
        registry.register("model_router", self._router or RuleRouter())
        registry.register("model_cache", self._cache or ExactCache())
        # 同源：host 与网关指向同一份注册表。
        host.registry = registry  # ModelRegistry 结构性满足 ProviderRegistry（register/has）
        return host

    @property
    def llm(self) -> LLMPort:
        """默认后端实例（装配核对用）。"""
        return self._llm

    @property
    def registry(self) -> ModelRegistry:
        """本能力持有的注册表（装配核对用）。"""
        return self._model_registry

    def gateway(self) -> ModelGateway:
        """构造模型网关（传入统一 registry，修复降级目标无法解析断点）。"""
        return ModelGateway(
            llm=self._llm, router=self._router, cache=self._cache,
            spec=self._spec, validator=self._validator,
            registry=self._model_registry,
        )

    def add_backend(self, name: str, llm: LLMPort) -> None:
        """注册降级目标后端（如 "backup"），供网关按 target 切换。"""
        self._model_registry.register_llm(name, llm)
