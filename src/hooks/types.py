"""R2 核心类型：共用注册表与可插拔协议基契约。

三体系正交后，本模块只保留跟扩展实现无关的数据契约：
- Registry：Provider 注册表（AOPContainer / LifecycleBus / 装配层共用同一实例，P6）。
- Provider / Capability / ProviderRegistry：可插拔基契约（R13 术语基线）。
- HookBusError：装配期错误（重名、缺 provider、冻结后写入）。

扩展点实现已拆分为 AOP 与 Lifecycle 两个体系；
how 维度见 src/aop/，when 维度见 hooks/lifecycle.py。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

__all__ = [
    "Capability",
    "HookBusError",
    "Provider",
    "ProviderRegistry",
    "Registry",
]


@runtime_checkable
class ProviderRegistry(Protocol):
    """最小统一注册表视图（供各装配组件持有）。

    R4 §6 要求网关与扩展容器共用一份注册表；此协议只约束 register/has，
    避免 hooks 反向依赖 harness.model（分层：hooks 不 import harness）。
    """
    name: str

    def register(self, name: str, provider: Provider) -> object: ...

    def has(self, name: str) -> bool: ...


@runtime_checkable
class Provider(Protocol):
    """所有可替换策略的基契约：带 name、可被替换。"""
    name: str


@runtime_checkable
class Capability(Protocol):
    """能力级开关：装了通过 install 生效，不装缺席（缺席即降级）。

    属性：
        name: 能力名。version: 版本。requires: 依赖的其他能力名。
    """
    name: str
    version: str
    requires: tuple[str, ...]

    def install(self, container: object, reg: Registry) -> None:
        """把本能力的增强器装到容器、provider 装到注册表。

        参数：
            container: AOPContainer（增强器注册目标）。
            reg: Provider 注册表。
        """
        ...


class HookBusError(Exception):
    """装配/执行错误（重名、缺 port、冻结后写入等）。

    保留类名以稳定错误码兼容（code="HOOKBUS_ERROR"），语义扩展为共用容器装配错误。
    """
    code = "HOOKBUS_ERROR"


class Registry:
    """注册表：name → Provider，支持注册/解析/替换；装配期可 freeze 拒绝后续写入。"""

    def __init__(self) -> None:
        """初始化空注册表，装配期 freeze 后拒绝任何写入。"""
        self._providers: dict[str, Provider] = {}
        self._frozen = False

    def register(self, name: str, provider: Provider) -> Registry:
        """注册 provider；重名报错（除非用 replace）；返回 self 支持链式。

        参数：
            name: provider 名。provider: 实现 Provider 契约的对象。
        返回：
            self（链式调用）。
        异常：
            HookBusError: 已冻结或重名。
        """
        if self._frozen:
            raise HookBusError("registry is frozen")
        if name in self._providers:
            raise HookBusError(f"duplicate provider name: {name}")
        self._providers[name] = provider
        return self

    def resolve(self, name: str) -> Provider:
        """解析 provider；缺失报错。

        参数：
            name: provider 名。
        返回：
            已注册的 provider。
        异常：
            HookBusError: 未注册。
        """
        if name not in self._providers:
            raise HookBusError(f"provider not registered: {name}")
        return self._providers[name]

    def replace(self, name: str, provider: Provider) -> Registry:
        """替换已注册的 provider；返回 self 支持链式。

        参数：
            name: 已存在的 provider 名。provider: 新实现。
        返回：
            self（链式调用）。
        异常：
            HookBusError: 已冻结。
        """
        if self._frozen:
            raise HookBusError("registry is frozen")
        self._providers[name] = provider
        return self

    def has(self, name: str) -> bool:
        """是否已注册该 name。"""
        return name in self._providers

    def names(self) -> Iterable[str]:
        """已注册名称列表（快照）。"""
        return list(self._providers)

    def freeze(self) -> Registry:
        """冻结注册表（装配期完成后），此后 register/replace 均拒绝。"""
        self._frozen = True
        return self
