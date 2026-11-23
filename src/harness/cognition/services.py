# purity-exempt-file: Planner/Executor/RePlanner/Reflect 协议签名含 ctx，实现按签名保留供装配期注入
"""认知服务确定性默认实现（R8 §4/§6）：Planner/Executor/RePlanner/ReflectStrategy。

用户铁律：默认路径确定性纯函数、不联网、不调真实 LLM；LLM 版由注入替换。
这些服务是 ⑥组件（不进 PORT_PROTOCOLS，不新增内核相位），产出结构化 DTO，
由 PlanExecute/Reflection 架构经 Effect 编排消费。
"""
from __future__ import annotations

from runtime import AgentState

from .contracts import Critique, Step, StepResult

__all__ = [
    "RulePlanner",
    "RuleExecutor",
    "RuleRePlanner",
    "ThresholdReflector",
]


class RulePlanner:
    """确定性规划：把目标按分隔符切成步骤（不调 LLM）。"""
    name = "rule_planner"

    def __init__(self, separator: str = ";") -> None:
        """构造规划器。

        Args:
            separator: 目标串的步骤分隔符。
        """
        self.separator = separator

    def plan(self, goal: str, ctx: AgentState | None = None) -> tuple[Step, ...]:
        """按分隔符切分目标为具名步骤（空段跳过）。"""
        parts = [p.strip() for p in goal.split(self.separator) if p.strip()]
        if not parts:
            parts = [goal.strip() or "noop"]
        return tuple(Step(action=p, step_id=f"s{i}") for i, p in enumerate(parts))


class RuleExecutor:
    """确定性执行：产出步骤回显结果（真实动作由调用方的 call/工具 Effect 兑现）。"""
    name = "rule_executor"

    def execute(self, step: Step, ctx: AgentState | None = None) -> StepResult:
        """执行单步：输出步骤 action（无副作用，供 PlanExecute 游标推进）。"""
        return StepResult(step_id=step.step_id, output=step.action, ok=True)


class RuleRePlanner:
    """确定性重规划：把失败原因作为前缀附加到剩余步骤（不调 LLM）。"""
    name = "rule_replanner"

    def replan(
        self, goal: str, steps: tuple[Step, ...], failures: tuple[str, ...],
        ctx: AgentState | None = None,
    ) -> tuple[Step, ...]:
        """保留未完成步骤并在首个步骤动作前标注失败原因。"""
        if not steps:
            return (Step(action=goal, step_id="s0"),)
        reason = failures[-1] if failures else "replan"
        head = steps[0]
        revised = Step(action=f"{head.action} [{reason}]", check=head.check, step_id=head.step_id)
        return (revised, *steps[1:])


class ThresholdReflector:
    """确定性反思：按关键符号/长度阈值判定 passed（max_rounds 由架构层控制）。"""
    name = "threshold_reflector"

    def __init__(self, min_len: int = 1, pass_marker: str = "PASS") -> None:
        """构造反思器。

        Args:
            min_len: 草稿最小长度。
            pass_marker: 草稿含此标记直接判通过。
        """
        self.min_len = min_len
        self.pass_marker = pass_marker

    def critique(self, draft: str, goal: str, ctx: AgentState | None = None) -> Critique:
        """判定草稿：含通过标记或达最小长度即 passed。"""
        text = draft or ""
        passed = self.pass_marker in text or len(text.strip()) >= self.min_len
        score = 1.0 if passed else 0.0
        comments = "" if passed else f"draft shorter than {self.min_len}"
        return Critique(passed=passed, score=score, comments=comments)
