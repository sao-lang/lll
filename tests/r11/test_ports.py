"""R11-T03 A1 集成契约：换实现上层零改（TDD RED → GREEN）。"""
from __future__ import annotations

from api import PortRegistry, PROTOCOL_NAMES
from compose import PORT_PROTOCOLS
from harness.model import ModelGateway
from tests.r11.conftest import StubArch


def test_port_names_fixed() -> None:
    """Port 注册名固定（∈ PORT_PROTOCOLS，不可运行时新增种类）。"""
    assert set(PROTOCOL_NAMES()) == set(PORT_PROTOCOLS)


def test_port_registry_register_resolve() -> None:
    """PortRegistry.register/resolve 基本契约。"""
    reg = PortRegistry()
    impl = object()
    reg.register("llm", impl)
    assert reg.resolve("llm") is impl


def test_port_unknown_name_rejected() -> None:
    """新增 Port 种类被拒（D-2：不可运行时新增 Port 种类）。"""
    reg = PortRegistry()
    try:
        reg.register("custom_port", object())
    except ValueError:
        return
    raise AssertionError("未知 Port 名应被拒")


def test_replace_port_upstream_zero_change() -> None:
    """换实现：resolve 拿到新实例，上层零改。"""
    reg = PortRegistry()
    a, b = object(), object()
    reg.register("llm", a)
    reg.replace("llm", b)
    assert reg.resolve("llm") is b
