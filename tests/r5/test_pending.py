"""R5-T10：装配期自动注册（@tool 入列 → register_pending 原子注册 + 清空）。"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from hooks import DangerLevel, Host, PortCtx, Registry, make_call
from harness.tools import (
    ToolsCapability,
    ToolRegistry,
    clear_pending,
    pending_tools,
    tool,
)


class AddArgs(BaseModel):
    """加法参数模型（extra=forbid 由模型配置保证）。"""
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int


def test_tool_decorator_enqueues_pending() -> None:
    """@tool 装饰时把 ToolFn 追加进待注册容器，调用签名不变。"""
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        return args.a + args.b

    assert any(fn.spec.name == "add" for fn in pending_tools())


def test_pending_tools_returns_snapshot() -> None:
    """pending_tools 返回 tuple 快照，外部持有后不受后续入列影响。"""
    @tool(name="first", danger=DangerLevel.SAFE)
    def first(args: AddArgs) -> int:
        return args.a

    snapshot = pending_tools()

    @tool(name="second", danger=DangerLevel.SAFE)
    def second(args: AddArgs) -> int:
        return args.b

    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1
    assert len(pending_tools()) == 2


def test_register_pending_registers_and_clears() -> None:
    """register_pending 把待注册项写入 registry 并清空容器；call 可统一调用。"""
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        return args.a + args.b

    registry = ToolRegistry()
    names = registry.register_pending()
    assert names == ["add"]
    assert [s.name for s in registry.catalog(level=2)] == ["add"]
    assert pending_tools() == ()

    result = asyncio.run(
        registry.call(make_call("add", {"a": 2, "b": 3}), PortCtx())
    )
    assert result.ok and result.value == 5


def test_register_pending_rejects_duplicate_without_partial_write() -> None:
    """重名整体失败：抛 ValueError、无半注册、pending 保留便于修正后重试。"""
    @tool(name="add", danger=DangerLevel.SAFE)
    def add(args: AddArgs) -> int:
        return args.a + args.b

    registry = ToolRegistry()
    registry.register(add.spec, add)  # 手动注册 "add"，与 pending 中同名

    @tool(name="other", danger=DangerLevel.SAFE)
    def other(args: AddArgs) -> int:
        return args.a

    before = pending_tools()
    with pytest.raises(ValueError):
        registry.register_pending()
    assert pending_tools() == before
    assert [s.name for s in registry.catalog(level=2)] == ["add"]


def test_register_pending_rejects_frozen_without_clearing() -> None:
    """freeze 后拒绝批量注册，pending 保留不改变。"""
    @tool(name="frozen_tool", danger=DangerLevel.SAFE)
    def frozen_tool(args: AddArgs) -> int:
        return args.a

    registry = ToolRegistry().freeze()
    with pytest.raises(RuntimeError):
        registry.register_pending()
    assert any(fn.spec.name == "frozen_tool" for fn in pending_tools())


def test_call_never_auto_registers() -> None:
    """运行期 call 永不自动注册：未注册工具仍返回 NOT_FOUND。"""
    @tool(name="ghost", danger=DangerLevel.SAFE)
    def ghost(args: AddArgs) -> int:
        return args.a

    registry = ToolRegistry()
    result = asyncio.run(
        registry.call(make_call("ghost", {"a": 1, "b": 2}), PortCtx())
    )
    assert not result.ok and result.error == "NOT_FOUND"
    assert len(pending_tools()) == 1


def test_install_flushes_pending_automatically() -> None:
    """ToolsCapability.install 先自动 flush pending，再注册各 Port。"""
    @tool(name="auto_add", danger=DangerLevel.SAFE)
    def auto_add(args: AddArgs) -> int:
        return args.a + args.b

    registry = ToolRegistry()
    cap = ToolsCapability(registry=registry)
    cap.install(Host(), Registry())
    assert "auto_add" in [s.name for s in registry.catalog(level=2)]
    assert pending_tools() == ()


def test_clear_pending_empties_container() -> None:
    """clear_pending 清空容器，供装配层与测试显式隔离。"""
    @tool(name="to_clear", danger=DangerLevel.SAFE)
    def to_clear(args: AddArgs) -> int:
        return args.a

    assert pending_tools()
    clear_pending()
    assert pending_tools() == ()
