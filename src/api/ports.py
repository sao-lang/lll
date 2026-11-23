"""R11 A1 集成契约：PortRegistry 封装 compose.Registry（固定 Port 名，禁运行时新增）。"""
from __future__ import annotations

from compose import PORT_PROTOCOLS, Provider, Registry

__all__ = ["PortRegistry", "PROTOCOL_NAMES"]


def PROTOCOL_NAMES() -> tuple[str, ...]:
    """返回可注册 Port 名（∈ PORT_PROTOCOLS，单一来源）。"""
    return tuple(PORT_PROTOCOLS)


class PortRegistry:
    """Port 注册表：只接受 PORT_PROTOCOLS 内的名字，换实现上层零改。"""
    name = "port_registry"

    def __init__(self) -> None:
        """建内部 Registry（不另造协议）。"""
        self._reg = Registry()

    def register(self, name: str, adapter: Provider) -> None:
        """注册适配器；未知 Port 名 ValueError（D-2 不可运行时新增种类）。"""
        if name not in PORT_PROTOCOLS:
            raise ValueError(f"unknown port: {name}")
        self._reg.register(name, adapter)

    def replace(self, name: str, adapter: Provider) -> None:
        """替换已注册适配器（换实现入口）。"""
        if name not in PORT_PROTOCOLS:
            raise ValueError(f"unknown port: {name}")
        self._reg.replace(name, adapter)

    def resolve(self, name: str) -> Provider:
        """解析适配器；未注册 KeyError。"""
        return self._reg.resolve(name)

    def has(self, name: str) -> bool:
        """是否已注册。"""
        return self._reg.has(name)
