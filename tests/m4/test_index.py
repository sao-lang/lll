"""M4-T03 三级渐进式披露：触发条件、token 上限、降级开关。"""
from __future__ import annotations

import pytest

from agentkit.harness.tools import ToolRegistry, ToolIndex
from agentkit.harness.tools.fakes import DictSkillProvider
from agentkit.ports import DangerLevel, ToolSpec
from agentkit.runtime import canonical


def bulk(count: int = 25, *, index_enabled: bool = True) -> ToolRegistry:
    registry = ToolRegistry(index_enabled=index_enabled)
    for i in range(count):
        registry.register(ToolSpec(
            name=f"t{i}", description=f"工具 {i}",
            parameters_schema={"type": "object", "properties": {
                f"p{j}": {"type": "string", "description": f"参数 {j} 的详细说明"}
                for j in range(6)
            }},
            danger=DangerLevel.SAFE,
        ), lambda args, c: "ok")
    return registry


def test_t03_l1_is_much_cheaper_than_full_injection():
    registry = bulk()
    index = ToolIndex(registry, DictSkillProvider(), threshold=20)
    assert index.should_index()
    l1 = index.catalog()
    assert all(s.parameters_schema == {} for s in l1)
    assert len(canonical(l1)) < len(canonical(registry.catalog(level=2))) / 3


def test_t03_l2_l3_load_on_demand_and_disclose():
    index = ToolIndex(bulk(), DictSkillProvider())
    index.catalog()  # L1 先披露
    l2 = index.load_schema(["t0", "t1"])
    assert [item["name"] for item in l2] == ["t0", "t1"]
    assert "properties" in l2[0]["parameters_schema"]
    assert index.load_body("python-expert").startswith("你是一名")
    assert index.entries[0].level == 1 and index.entries[1].level == 2
    assert index.entries[-1].level == 3 and index.entries[-1].loaded_at == 4

    fresh = ToolIndex(bulk(), DictSkillProvider())
    pack = fresh.disclose(names=["t3"], skills=["python-expert"])
    assert set(pack) == {"L1", "L2", "L3", "token_cost"}
    assert pack["token_cost"] > 0 and "python-expert" in pack["L3"]


def test_t03_token_budgets_and_missing_provider():
    index = ToolIndex(bulk(), l1_token_budget=1)
    with pytest.raises(ValueError, match="L1 catalog exceeds"):
        index.catalog()
    index2 = ToolIndex(bulk(), l2_token_budget=1)
    with pytest.raises(ValueError, match="L2 schema"):
        index2.load_schema(["t0"])
    with pytest.raises(ValueError, match="SkillProvider"):
        ToolIndex(bulk()).load_body("x")


def test_t03_small_registry_can_disable_index():
    registry = bulk(5, index_enabled=False)
    index = ToolIndex(registry, threshold=20)
    assert not index.should_index()
    assert all(s.parameters_schema for s in registry.catalog(level=1))  # 全量注入降级


def test_t03_static_check_requires_index_for_many_tools():
    registry = bulk(25, index_enabled=False)
    assert any("progressive disclosure disabled" in e for e in registry.static_check())
