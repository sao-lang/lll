# purity-exempt-file: EvalPort.score / Judge.score 协议签名含 ctx，生产 Judge 实现消费本默认不读
"""R10 确定性评测（B.1.2 EvalPort→RuleEval + Judge 默认实现）。

履约 compose.ports.EvalPort 源码签名：
    async score(case_id: str, output: DynamicValue, ctx: PortCtx) -> float

三层评测：确定性不变量（RuleEval）/ 黄金轨迹（GoldenJudge）/ 生产采样（回归入口）。
不调真实 LLM；LLM-as-judge 由注入 Judge 替换（本默认不联网）。
"""
from __future__ import annotations

from compose import EvalPort, PortCtx
from runtime import DynamicValue

from .types import EvalCase, Score, SuiteReport

__all__ = ["RuleEval", "GoldenJudge"]


class RuleEval(EvalPort):
    """EvalPort 默认实现：确定性断言评分（golden 集，零 LLM 调用）。"""
    name = "rule_eval"

    def __init__(self, cases: dict[str, EvalCase] | None = None) -> None:
        """构造评测器。

        Args:
            cases: case_id → EvalCase 映射；None 时按 output 与 expected 直接比较
                   （调用方传入 expected 的场景）。
        """
        self._cases = dict(cases or {})

    async def score(self, case_id: str, output: DynamicValue, ctx: PortCtx) -> float:
        """评分：should_refuse 用例判拒答；否则 output == expected 得 1.0。"""
        case = self._cases.get(case_id)
        if case is None:
            # 无注册用例：把 output 当 (expected, actual) 二元组比较（确定性兜底）。
            if isinstance(output, (tuple, list)) and len(output) == 2:
                return 1.0 if output[0] == output[1] else 0.0
            return 0.0
        if case.should_refuse:
            text = str(output).strip().upper()
            return 1.0 if "REFUSE" in text else 0.0
        return 1.0 if output == case.expected else 0.0

    def add_case(self, case: EvalCase) -> None:
        """注册评测用例（golden 集）。"""
        self._cases[case.case_id] = case

    async def regression(self, suite: str, ctx: PortCtx) -> SuiteReport:
        """回归套件：用录制/采样得到的 case.actual 评分，产结构化报告（CI gate 消费）。

        未采集 actual 的用例记 0 分（fail-closed，不编造通过）。
        """
        scores = [await self.score(cid, case.actual, ctx)
                  for cid, case in self._cases.items()]
        passed = sum(1 for s in scores if s >= 0.5)
        average = sum(scores) / len(scores) if scores else 0.0
        return SuiteReport(suite=suite, total=len(scores), passed=passed, average=average)


class GoldenJudge:
    """Judge 默认实现：黄金轨迹匹配（确定性不调 LLM）。"""
    name = "golden_judge"

    async def score(self, case: EvalCase, output: DynamicValue,
                    ctx: PortCtx) -> Score:
        """轨迹全等得 1.0，缺步/多步按比例扣（交集长度 / 并集长度）。"""
        expected = tuple(case.expected_trajectory)
        actual = tuple(output) if isinstance(output, (list, tuple)) else ()
        if not expected:
            return Score(value=1.0 if actual == () else 0.0)
        if actual == expected:
            return Score(value=1.0, reason="exact_trajectory")
        common = len(set(expected) & set(actual))
        union = len(set(expected) | set(actual)) or 1
        return Score(value=common / union, reason="partial_trajectory")
