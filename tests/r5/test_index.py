"""R5-T02：三级披露 ToolIndex —— L1 无 schema，超预算报错，MCP 合并无差别。"""
from __future__ import annotations

from compose import DangerLevel, DisclosurePacket, PortCtx, SkillManifest, ToolSpec, make_call
from harness.tools import ToolIndex, ToolRegistry, tool


def _spec(name: str, *, danger: DangerLevel = DangerLevel.SAFE) -> ToolSpec:
    return ToolSpec(
        name=name, description=f"{name} 一句话描述",
        parameters_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        danger=danger, source="local",
    )


class _StubSkillPort:
    """SkillPort 测试替身：body 按名返回正文，命中 boom 集合则模拟注入检测抛 ValueError。"""

    name = "stub_skill"
    _bodies: dict[str, str]
    _boom: set[str]

    def __init__(self, bodies: dict[str, str], *, boom: set[str] | None = None) -> None:
        """构造替身：bodies 为 name→正文，boom 为需抛注入异常的名字集合。"""
        self._bodies = bodies
        self._boom = boom if boom is not None else set()

    def discover(self) -> list[SkillManifest]:
        """返回按 bodies 构造的技能清单。"""
        return [SkillManifest(name=n, version="1.0", description="d") for n in self._bodies]

    def catalog(self) -> list[SkillManifest]:
        """返回技能目录（与 discover 同形）。"""
        return self.discover()

    def body(self, name: str) -> str:
        """返回正文；命中 boom 则模拟注入检测拒绝。"""
        if name in self._boom:
            raise ValueError("正文命中注入检测")
        return self._bodies[name]


def test_l3_body_loaded_from_injected_skill_port() -> None:
    """注入 SkillPort 后技能正文进入 l3，且计入 token_cost。"""
    stub = _StubSkillPort({"review": "这是审查技能的正文内容" * 10})
    index = ToolIndex(ToolRegistry(), skills=stub)
    manifest = SkillManifest(name="review", version="1.0", description="d")
    packet = index.disclose([], [manifest])
    assert packet.l3["review"] == stub.body("review")
    assert packet.token_cost > 0


def test_l3_without_skill_port_stays_empty() -> None:
    """无 SkillPort 注入时 l3 仍为空（保持协议降级行为）。"""
    index = ToolIndex(ToolRegistry())
    manifest = SkillManifest(name="review", version="1.0", description="d")
    assert index.disclose([], [manifest]).l3 == {}


def test_l3_injection_body_folded_without_raise() -> None:
    """正文命中注入（body 抛 ValueError）时折叠为空串，不抛穿披露链。"""
    stub = _StubSkillPort({"evil": "正文"}, boom={"evil"})
    index = ToolIndex(ToolRegistry(), skills=stub)
    manifest = SkillManifest(name="evil", version="1.0", description="d")
    packet = index.disclose([], [manifest])
    assert packet.l3["evil"] == ""


def test_l1_has_no_full_schema() -> None:
    """L1 目录条目的 parameters_schema 必须为空（红线：不进完整 schema）。"""
    registry = ToolRegistry()
    registry.register(_spec("alpha"), lambda **k: None)
    registry.catalog  # noqa
    index = ToolIndex(registry)
    packet = index.disclose(names=[], skills=[])
    assert isinstance(packet, DisclosurePacket)
    assert all(s.parameters_schema == {} for s in packet.l1)
    assert all(s.parameters_schema != {} for s in packet.l2) or packet.l2 == ()


def test_l2_discloses_hit_schema() -> None:
    """命中意图的工具进 L2，携带完整 schema。"""
    registry = ToolRegistry()
    registry.register(_spec("alpha"), lambda **k: None)
    registry.register(_spec("beta"), lambda **k: None)
    index = ToolIndex(registry)
    packet = index.disclose(names=["alpha"], skills=[])
    assert [s.name for s in packet.l2] == ["alpha"]
    assert packet.l2[0].parameters_schema


def test_mcp_and_local_same_registry(ctx: PortCtx) -> None:
    """本地工具与 MCP 工具注入同一 Registry，catalog/call 无差别。"""
    registry = ToolRegistry()
    local = _spec("mcp__srv__remote", danger=DangerLevel.SAFE)
    local = local.model_copy(update={"source": "mcp"})
    registry.register(local, lambda **k: "remote-ok")
    assert "mcp__srv__remote" in [s.name for s in registry.catalog()]
    result = __import__("asyncio").run(
        registry.call(make_call("mcp__srv__remote", {}), ctx)
    )
    assert result.ok and result.value == "remote-ok" and result.meta["source"] == "mcp"


def test_budget_exceeded_raises() -> None:
    """披露 token 预算超限时显式拒绝（不静默注入）。"""
    registry = ToolRegistry()
    for i in range(30):
        registry.register(_spec(f"tool_{i}"), lambda **k: None)
    index = ToolIndex(registry, l1_token_budget=10, force_threshold=20)
    try:
        index.disclose(names=[], skills=[])
    except ValueError as e:
        assert "预算" in str(e) or "budget" in str(e)
    else:
        raise AssertionError("超预算应报错")


def test_over_20_forces_disclosure() -> None:
    """合集 >20 条强制披露：names 为空时 L2 必须为空（不能全量注入）。"""
    registry = ToolRegistry()
    for i in range(25):
        registry.register(_spec(f"tool_{i}"), lambda **k: None)
    index = ToolIndex(registry, l1_token_budget=100000)
    packet = index.disclose(names=[], skills=[])
    assert packet.l2 == ()
