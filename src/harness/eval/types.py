"""R10 评测 DTO（R10 §5）：EvalCase / Score / SuiteReport。

确定性优先：默认 RuleEval/GoldenJudge 不调真实 LLM；LLM-as-judge 由注入替换。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from runtime import DynamicValue

__all__ = ["EvalCase", "Score", "SuiteReport"]


@dataclass(frozen=True, slots=True)
class EvalCase:
    """评测用例：input/expected/expected_trajectory/should_refuse/actual。

    actual = 录制回放或生产采样得到的实际输出（回放底座=R10 §2）；
    regression 用它评分，未采集时为 None（该用例记 0 分，不编造通过）。
    """
    case_id: str
    input: DynamicValue
    expected: DynamicValue = None
    expected_trajectory: tuple[DynamicValue, ...] = ()
    should_refuse: bool = False
    actual: DynamicValue = None


@dataclass(frozen=True, slots=True)
class Score:
    """评分结果：value 0~1 + 可选理由（不调 LLM 时 reason 为空）。"""
    value: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """套件报告：用例数 + 通过数 + 平均分。"""
    suite: str
    total: int = 0
    passed: int = 0
    average: float = 0.0
    details: dict[str, DynamicValue] = field(default_factory=dict)
