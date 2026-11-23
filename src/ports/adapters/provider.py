"""R11 通用 LLM provider 能力包：端点配置 → LLMPort 实例的可插拔工厂。

为何单独一层（用户设计意图）：llm provider 是最基础组件，多厂商端点配置
（base_url/api_key/model）与模型能力（路由/缓存/成本/降级）职责不同——本模块
只做"配置→实例"，产出的实例交给 ModelRegistry/ModelCapability 消费。

可插拔点：
- LLMProviderFactory 协议：任何厂商适配器只要实现该工厂签名就能注册；
- 默认 OpenAI-compatible 工厂（OpenAICompatLLM），覆盖 DeepSeek/Moonshot/vLLM
  等 OpenAI 兼容端点；非兼容原生协议（Anthropic Messages 等）须用户注入自定义工厂。
- 密钥只走构造参数/环境变量，不落文件、不进模板（安全红线）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from compose import LLMPort, Registry
from compose.registry import HookBusError
from harness.model import ModelRegistry

__all__ = ["LLMProviderFactory", "LLMProviderCapability", "openai_http"]


@runtime_checkable
class LLMProviderFactory(Protocol):
    """Provider 工厂协议：端点配置 → LLMPort 实例（用户可注入任意厂商实现）。"""

    name: str

    def __call__(self, *, base_url: str, api_key: str, model: str) -> LLMPort:
        """按端点配置构造 provider 实例（履约 LLMPort：complete/stream）。"""
        # purity-exempt: Protocol 方法体是接口语法；参数由实现方消费，此处不可有实现
        raise NotImplementedError  # 协议方法：实现方必须覆写（非可运行默认路径）
        ...  # purity-exempt: Protocol 方法体是接口语法，非占位实现


def openai_http(*, base_url: str, api_key: str, model: str) -> LLMPort:
    """默认工厂：OpenAI-compatible HTTP 适配器（DeepSeek/Moonshot/OpenAI/vLLM）。"""
    from ports.adapters.llm import OpenAICompatLLM
    return OpenAICompatLLM(base_url=base_url, api_key=api_key, model=model)


class LLMProviderCapability:
    """LLM provider 能力包：注册具名端点配置，按需构造并缓存实例。

    与 ModelCapability 分层：本包只负责"配置 → 可调用的 LLMPort"，
    路由/缓存/成本/降级仍由 ModelCapability + ModelGateway 负责。
    """

    name = "llm_provider"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, *, default_factory: Callable[..., LLMPort] | None = None) -> None:
        """构造：缺省工厂为 OpenAI-compatible 适配器。"""
        self._default_factory: Callable[..., LLMPort] = default_factory or openai_http
        self._configs: dict[str, dict[str, str]] = {}
        self._factories: dict[str, Callable[..., LLMPort]] = {}
        self._instances: dict[str, LLMPort] = {}

    def register(
        self,
        name: str,
        *,
        factory: Callable[..., LLMPort] | None = None,
        base_url: str,
        api_key: str,
        model: str,
    ) -> LLMPort:
        """注册端点配置；同名幂等（返回已有实例），异配置拒绝（fail-closed）。"""
        config = {"base_url": base_url, "api_key": api_key, "model": model}
        old = self._configs.get(name)
        if old is not None:
            if old != config:
                raise HookBusError(
                    f"provider {name!r} 已注册不同端点配置（拒绝静默覆盖）")
            return self._instances[name]
        self._configs[name] = config
        self._factories[name] = factory or self._default_factory
        instance = self._factories[name](**config)
        self._instances[name] = instance
        return instance

    def resolve(self, name: str) -> LLMPort | None:
        """解析已构造的 provider 实例（未注册返回 None）。"""
        return self._instances.get(name)

    def names(self) -> tuple[str, ...]:
        """已注册 provider 名（按注册顺序）。"""
        return tuple(self._configs)

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:  # purity-exempt: container 为 Capability 协议签名（未消费）
        """Capability 协议：把构造好的 provider 注册进通用 Registry。"""
        _ = container  # 协议入参：provider 经 resolve 消费，不持有容器
        if reg is None:
            return
        for name, instance in self._instances.items():
            reg.register(f"llm_provider:{name}", instance)

    def install_into(self, registry: ModelRegistry) -> None:
        """把 provider 实例登记进 ModelRegistry（供网关多后端降级真消费）。

        对 ModelRegistry 而言这些是"已构造实例"，走 register_llm 而非端点表——
        端点表只存配置、不构造实例（此前无人消费的缺口由本包关闭）。
        """
        for name, instance in self._instances.items():
            if not isinstance(instance, LLMPort):
                raise TypeError(
                    f"provider {name!r} 未履约 LLMPort 协议（complete/stream）")
            registry.register_llm(name, instance)
