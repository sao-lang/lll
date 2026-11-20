"""R4 模型端点注册表：把"模型名"解析到真实后端实例。

为何需要：此前降级目标只到裁决层就断了，且一个 LLMPort 实例固定一个
base_url/api_key——跨服务商降级无效。本模块提供"名字→端点/实例"映射，供网关按
路由/降级目标切换真实后端。框架只定义协议+内存默认，具体多端点由装配层注册。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from compose import HookBusError, LLMPort, Provider

__all__ = ["ModelEndpoint", "ModelRegistry", "InMemoryModelRegistry"]


@dataclass(frozen=True, slots=True)
class ModelEndpoint:
    """一个模型端点的连接信息（供适配器构造；密钥不进 trace/日志）。

    字段：
        name: 注册名（路由/降级 target 用）。
        base_url: OpenAI-compatible 端点根。
        api_key: 密钥（仅进 Authorization 头）。
        model: 请求体里的模型名。
    """

    name: str
    base_url: str
    api_key: str
    model: str


@runtime_checkable
class ModelRegistry(Protocol):
    """模型端点注册表协议：按名字解析端点与后端实例。"""

    name: str

    def register(
        self, name: str, provider: Provider | None = None, *,
        base_url: str | None = None, api_key: str | None = None, model: str | None = None,
    ) -> None:
        """注册具名 provider（设计 §5.3）或端点连接信息（既有调用形态）。

        二选一：给 provider 走 provider 表；给 base_url/api_key/model 走端点表。
        """
        ...

    def register_llm(self, name: str, llm: LLMPort) -> None:
        """注册已构造好的后端实例（供降级直接切换，跳过重建适配器）。"""
        ...

    def resolve(self, name: str) -> Provider | ModelEndpoint | None:
        """按名字解析 provider（优先）或端点；未注册返回 None。"""
        ...

    def resolve_llm(self, name: str) -> LLMPort | None:
        """按名字解析后端实例；未注册返回 None。"""
        ...

    def register_provider(self, name: str, provider: Provider) -> None:
        """注册具名 provider（llm/router/cache），供两表统一；同名幂等/异实例拒绝。"""
        ...

    def has(self, name: str) -> bool:
        """名字是否已注册（端点/后端/provider 任一）。"""
        ...


class InMemoryModelRegistry:
    """默认注册表：进程内字典，装配期注册、运行期解析。"""

    name = "in_memory_model_registry"

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._endpoints: dict[str, ModelEndpoint] = {}
        self._llms: dict[str, LLMPort] = {}
        self._providers: dict[str, Provider] = {}

    def register(
        self, name: str, provider: Provider | None = None, *,
        base_url: str | None = None, api_key: str | None = None, model: str | None = None,
    ) -> None:
        """注册 provider（同名幂等/异实例拒绝）或端点连接信息（同名覆盖）。

        两种形态互斥：传 provider 走统一 provider 表（两表统一用）；
        传 base_url/api_key/model 走端点表（既有兼容调用）。
        """
        if provider is not None:
            self.register_provider(name, provider)
            return
        if base_url is None or api_key is None or model is None:
            raise HookBusError("register requires either provider or base_url/api_key/model")
        self._endpoints[name] = ModelEndpoint(
            name=name, base_url=base_url, api_key=api_key, model=model
        )

    def register_llm(self, name: str, llm: LLMPort) -> None:
        """注册后端实例：同名同实例幂等，同名异实例拒绝（R4 §5.3）。"""
        existing = self._llms.get(name)
        if existing is llm:
            return  # 同名同实例：幂等，不抛
        if existing is not None:
            raise HookBusError(f"different instance already registered for llm: {name}")
        self._llms[name] = llm

    def register_provider(self, name: str, provider: Provider) -> None:
        """注册具名 provider（llm/router/cache）：同名同实例幂等 / 异实例拒绝。"""
        existing = self._providers.get(name)
        if existing is provider:
            return
        if existing is not None:
            raise HookBusError(f"different instance already registered for provider: {name}")
        self._providers[name] = provider

    def resolve(self, name: str) -> Provider | ModelEndpoint | None:
        """解析 provider（优先）或端点；未注册返回 None。"""
        provider = self._providers.get(name)
        if provider is not None:
            return provider
        return self._endpoints.get(name)

    def resolve_llm(self, name: str) -> LLMPort | None:
        """解析后端实例；未注册返回 None。"""
        return self._llms.get(name)

    def resolve_provider(self, name: str) -> Provider | None:
        """解析具名 provider；未注册返回 None。"""
        return self._providers.get(name)

    def has(self, name: str) -> bool:
        """名字是否已注册（端点/后端/provider 任一）。"""
        return name in self._endpoints or name in self._llms or name in self._providers
