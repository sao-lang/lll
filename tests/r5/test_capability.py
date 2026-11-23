"""R5-T08：ToolsCapability 装配 + 模块闭环（stateless 校验）。"""
from __future__ import annotations

from pathlib import Path

from compose import DisclosureIndex, PortCtx, Registry, ToolPort
from harness.tools import MCPManager, ToolIndex, ToolRegistry, ToolsCapability, tool
from harness.skills import FileSkillProvider

from .conftest import FakeAudit, FakeHuman, FakeSandbox, FakeTransport


def test_capability_metadata() -> None:
    """ToolsCapability 具备 R13 Capability 三要素（name/version/requires）。"""
    cap = ToolsCapability()
    assert cap.name == "tools" and cap.version and cap.requires == ()


def test_capability_builds_defaults() -> None:
    """不注入时构造默认 ToolRegistry + ToolIndex；MCP/Skills 缺席为 None。"""
    cap = ToolsCapability()
    assert isinstance(cap.registry(), ToolRegistry)
    assert isinstance(cap.index(), ToolIndex)
    assert cap.manager() is None and cap.skills() is None


def test_capability_with_injected_parts(tmp_path: Path) -> None:
    """注入 R9 三 Port + MCP transport + Skill provider，装配期履约校验。"""
    _make = tmp_path / "code"
    _make.mkdir()
    (_make / "manifest.json").write_text(
        '{"name":"code","version":"1.0","description":"d","triggers":[]}', encoding="utf-8"
    )
    (_make / "SKILL.md").write_text("正文", encoding="utf-8")

    reg = ToolRegistry(sandbox=FakeSandbox(), human=FakeHuman(), audit=FakeAudit())
    transport = FakeTransport()
    mgr = MCPManager(reg, transport)
    skills = FileSkillProvider(tmp_path)
    cap = ToolsCapability(registry=reg, mcp=mgr, skills=skills)
    assert cap.manager() is mgr and cap.skills() is skills
    assert isinstance(cap.index(), DisclosureIndex)


def test_static_check_flags_missing_danger(tmp_path: Path) -> None:
    """装配期 static_check：danger 缺失/重名/高危无沙盒 全被列出。"""
    reg = ToolRegistry()
    from compose import ToolSpec
    # danger=None 的 spec 直接构造（绕过装饰器的必填）
    bad = ToolSpec(name="bad", description="x", parameters_schema={}, danger=None)
    reg.register(bad, lambda **k: None)
    problems = reg.static_check()
    assert any("danger" in p for p in problems)


def test_registry_is_tool_port() -> None:
    """默认实现履约 R2 ToolPort（isinstance）。"""
    assert isinstance(ToolRegistry(), ToolPort)


def test_install_registers_all_present_ports(tmp_path: Path) -> None:
    """install 按 R2 Capability 契约把持有的 port 注册进 Registry，可按规范端口名取回。"""
    reg = ToolRegistry()
    mgr = MCPManager(reg, FakeTransport())
    skill_dir = tmp_path / "code"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(
        '{"name":"code","version":"1.0","description":"d","triggers":[]}', encoding="utf-8"
    )
    skills = FileSkillProvider(tmp_path)
    cap = ToolsCapability(registry=reg, mcp=mgr, skills=skills)
    provider_reg = Registry()
    cap.install(object(), provider_reg)
    assert provider_reg.resolve("tool") is reg
    assert provider_reg.resolve("disclosure") is cap.index()
    assert provider_reg.resolve("mcp") is mgr
    assert provider_reg.resolve("skill") is skills


def test_install_skips_absent_optional_ports() -> None:
    """MCP/Skills 缺席时 install 不注册这两项，仍注册 tool/disclosure。"""
    cap = ToolsCapability()
    provider_reg = Registry()
    cap.install(object(), provider_reg)
    assert set(provider_reg.names()) == {"tool", "disclosure"}
    assert isinstance(provider_reg.resolve("tool"), ToolPort)
    assert isinstance(provider_reg.resolve("disclosure"), DisclosureIndex)
