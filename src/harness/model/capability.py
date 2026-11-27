"""R4 能力装配：ModelCapability 构造网关并持有共用 ModelRegistry。

降级目标后端通过 add_backend 注册到同一 ModelRegistry，供网关按 typed 目标切换；
容器/网关/能力共用这份注册表（P6 两表统一，载体换为共用 Registry）。
"""
from __future__ import annotations

from compose.tool_layer import layer_program
from compose import LifecycleBus, Registry
from runtime import CallFn, Program

from .ports import LLMPortWithUsage

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
        llm: LLMPortWithUsage,
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

    def build(self) -> Program:
        """ToolLayer 外壳（规格 P4）：产出"用模型"的 Program 工厂。

        内部路由/缓存/成本防护不变（由 gateway 消费），这里只给 R1 一个
        领域无知的 Program（yield Invoke(port="llm") + Terminate）。
        """
        return layer_program("llm", payload={"layer": "model"})()

    def install(self, container: object | None = None, reg: Registry | None = None) -> Registry:
        """统一装配：把 llm/router/cache 注册进共用 Provider 注册表。

        参数：
            container: 可选；若给定且有 registry 属性，指向同一份注册表（两表统一）。
            reg: 注入的注册表；缺省用本能力持有的实例。
        返回：
            共用注册表（支持链式）。

        缺陷修复（2026-11-28，IronForge 实测 #1）：门面 `_install_caps` 传入的
        compose Registry **没有 resolve_llm**（显式路由必需），原实现直接覆盖
        `_model_registry`，路由到具名后端时 gateway 必然 AttributeError（且 ReAct
        会把失败吞成空结论）。现在：具备 resolve_llm 的模型域注册表才允许接管；
        compose Registry 仅作 facade 侧共享登记，gateway 解析不受影响。
        """
        if reg is not None and hasattr(reg, "resolve_llm"):
            self._model_registry = reg  # type: ignore[assignment] # noqa — 具备 resolve_llm 即模型域注册表（含 ModelRegistry 实现）
        registry = self._model_registry
        self._register_once(registry, "llm", self._llm)
        self._register_once(registry, "model_router", self._router or RuleRouter())
        self._register_once(registry, "model_cache", self._cache or ExactCache())
        if reg is not None and not hasattr(reg, "resolve_llm"):
            # facade 侧共享登记（_install_caps 仍可枚举做端口推断；"llm" 由门面跳过）
            self._register_once(reg, "llm", self._llm)
            self._register_once(reg, "model_router", self._router or RuleRouter())
            self._register_once(reg, "model_cache", self._cache or ExactCache())
        if container is not None and hasattr(container, "registry"):
            setattr(container, "registry", registry)
        return registry  # type: ignore[return-value]

    @staticmethod
    def _register_once(registry: object, name: str, provider: object) -> None:
        """同名同实例幂等登记；异实例优先替换（compose Registry 支持）否则保留首个。

        多次 install（门面每装配期调用）不应因 router/cache 每次新实例而炸。
        """
        try:
            existing = registry.resolve(name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — 未注册的 raise 形态归一为 None
            existing = None
        if existing is provider:
            return
        if existing is None:
            registry.register(name, provider)  # type: ignore[attr-defined]
            return
        # 规则 6 例外：具名可选能力探测（registry 是否支持 replace），非动态键读取。
        replace = getattr(registry, "replace", None)
        if callable(replace):
            replace(name, provider)

    @property
    def llm(self) -> LLMPortWithUsage:
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

    def dispatch(self) -> CallFn:
        """P4 dispatch 入口（§2.5 形态）：直接吃 (effect, ctx)，不构造 Invocation。

        模型能力只有 llm 一个 port，故 gateway.as_dispatch() 本身即为该 port 的
        dispatch 实现；组合层可直接注入 R1 Runner（不经可靠性执行器），验证
        "call 按 port 分派 + typed target 从 ctx.meta 读" 主线。
        """
        return self.gateway().as_dispatch()

    def add_backend(self, name: str, llm: LLMPortWithUsage) -> None:
        """注册降级目标后端（如 "backup"），供网关按 typed 目标切换。"""
        self._model_registry.register_llm(name, llm)
