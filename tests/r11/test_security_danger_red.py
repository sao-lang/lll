"""RED：门面 register_tool 不得静默把架构声明的 CRITICAL 降级为 SAFE。

裂缝：register_tool 用 `kwargs.get("danger") or SAFE`，用户只提供处理器函数时，
架构 ToolSpec 声明的 CRITICAL 被覆盖为 SAFE → 执行时绕过审批/沙盒/审计（fail-open）。
修后：省略 danger 时沿用架构声明的危险级别；显式传入仍以显式为准（取更严者）。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, ToolSpec
from harness.tools import clear_pending


def test_red_register_tool_inherits_arch_danger() -> None:
    clear_pending()
    arch_spec = ToolSpec(name="danger_tool", danger=DangerLevel.CRITICAL)
    agent = Agent(ReAct([arch_spec], llm="fake"))
    agent.register_tool("danger_tool", lambda: "OK")  # 省略 danger
    specs = {spec.name: spec for spec, _ in agent._tools}
    assert specs["danger_tool"].danger is DangerLevel.CRITICAL, \
        f"省略 danger 应沿用架构声明 CRITICAL，实际 {specs['danger_tool'].danger}"


def test_red_explicit_danger_still_respected() -> None:
    clear_pending()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.register_tool("t", lambda: "OK", danger=DangerLevel.HIGH)
    specs = {spec.name: spec for spec, _ in agent._tools}
    assert specs["t"].danger is DangerLevel.HIGH, \
        f"显式 danger 应保留，实际 {specs['t'].danger}"


def test_red_no_arch_spec_defaults_safe() -> None:
    clear_pending()
    agent = Agent(ReAct([], llm="fake"))
    agent.register_tool("t", lambda: "OK")
    specs = {spec.name: spec for spec, _ in agent._tools}
    assert specs["t"].danger is DangerLevel.SAFE, \
        f"架构无声明时默认 SAFE，实际 {specs['t'].danger}"
