"""R8-T02 规划执行：确定性 Planner/Executor/RePlanner 游标正确。"""
from __future__ import annotations

from harness.cognition import RuleExecutor, RulePlanner, RuleRePlanner, Step


def test_planner_splits_goal_into_steps() -> None:
    """规划器把目标切为具名步骤，空段跳过。"""
    planner = RulePlanner()
    steps = planner.plan("gather data; analyze; report")
    assert [s.action for s in steps] == ["gather data", "analyze", "report"]
    assert [s.step_id for s in steps] == ["s0", "s1", "s2"]


def test_executor_advances_each_step() -> None:
    """执行器逐 step 调度，output 回显当前步骤，游标不跳步。"""
    planner = RulePlanner()
    executor = RuleExecutor()
    steps = planner.plan("a;b;c")
    outputs = []
    for step in steps:
        result = executor.execute(step)
        assert result.ok and result.step_id == step.step_id
        outputs.append(result.output)
    assert outputs == ["a", "b", "c"]


def test_replanner_prefixes_failure() -> None:
    """重规划把失败原因写入首步，保留剩余步骤。"""
    planner = RulePlanner()
    steps = planner.plan("a;b")
    revised = RuleRePlanner().replan("a;b", steps, ("tool_timeout",))
    assert revised[0].action == "a [tool_timeout]"
    assert revised[1] == steps[1]


def test_planner_empty_goal_fallback() -> None:
    """空目标产 noop 步骤（不产空 tuple，避免下游游标卡死）。"""
    steps = RulePlanner().plan("")
    assert len(steps) == 1 and steps[0].action == "noop"
