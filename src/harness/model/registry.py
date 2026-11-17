"""R4 模型端点注册表：把"模型名"解析到真实后端实例（v1.5 降级链补齐）。

为何需要：此前 DEGRADE target 只到 Decision 就断了，且一个 LLMPort 实例固定一个
base_url/api_key——跨服务商降级无效。本模块提供"名字→端点/实例"映射，供网关按
路由/降级目标切换真实后端。框架只定义协议+内存默认，具体多端点由装配层注册。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hooks import LLMPort

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
        self, name: str, *, base_url: str, api_key: str, model: str
    ) -> None:
        """注册端点连接信息。"""
        ...

    def register_llm(self, name: str, llm: LLMPort) -> None:
        """注册已构造好的后端实例（供降级直接切换，跳过重建适配器）。"""
        ...

    def resolve(self, name: str) -> ModelEndpoint | None:
        """按名字解析端点；未注册返回 None。"""
        ...

    def resolve_llm(self, name: str) -> LLMPort | None:
        """按名字解析后端实例；未注册返回 None。"""
        ...


class InMemoryModelRegistry:
    """默认注册表：进程内字典，装配期注册、运行期解析。"""

    name = "in_memory_model_registry"

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._endpoints: dict[str, ModelEndpoint] = {}
        self._llms: dict[str, LLMPort] = {}

    def register(
        self, name: str, *, base_url: str, api_key: str, model: str
    ) -> None:
        """注册端点连接信息（同名覆盖）。"""
        self._endpoints[name] = ModelEndpoint(
            name=name, base_url=base_url, api_key=api_key, model=model
        )

    def register_llm(self, name: str, llm: LLMPort) -> None:
        """注册后端实例（同名覆盖）。"""
        self._llms[name] = llm

    def resolve(self, name: str) -> ModelEndpoint | None:
        """解析端点；未注册返回 None。"""
        return self._endpoints.get(name)

    def resolve_llm(self, name: str) -> LLMPort | None:
        """解析后端实例；未注册返回 None。"""
        return self._llms.get(name)
