"""R4 能力装配（ARCH-v5）：ModelCapability 构造网关并持有 ModelRegistry。

降级目标后端通过 add_backend 注册到同一 ModelRegistry，供网关按 target 切换。
"""
from __future__ import annotations

from hooks import LLMPort

from .cache import CachePort
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

    def gateway(self) -> ModelGateway:
        """构造模型网关（传入 registry，修复降级目标无法解析断点）。"""
        return ModelGateway(
            llm=self._llm, router=self._router, cache=self._cache,
            spec=self._spec, validator=self._validator,
            registry=self._model_registry,
        )

    def add_backend(self, name: str, llm: LLMPort) -> None:
        """注册降级目标后端（如 "backup"），供网关按 target 切换。"""
        self._model_registry.register_llm(name, llm)
