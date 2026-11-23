"""R10-T05 三层评测：确定性断言 / 黄金轨迹 / 生产采样，不调真实 LLM。"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.eval import EvalCase, GoldenJudge, RuleEval


def _ctx() -> PortCtx:
    return PortCtx(session_id="s10", run_id="r10")


def test_t05_rule_eval_exact_match() -> None:
    """RuleEval.score：输出与 expected 相等得 1.0（确定性断言层）。"""
    evaluator = RuleEval()
    case = EvalCase(case_id="c1", input="q", expected="a",
                    expected_trajectory=(), should_refuse=False)
    evaluator.add_case(case)
    assert asyncio.run(evaluator.score("c1", "a", _ctx())) == 1.0
    assert asyncio.run(evaluator.score("c1", "b", _ctx())) == 0.0


def test_t05_should_refuse() -> None:
    """should_refuse 用例：拒答得 1.0，未拒答得 0.0。"""
    evaluator = RuleEval()
    case = EvalCase(case_id="c2", input="danger", expected="",
                    expected_trajectory=(), should_refuse=True)
    cases = {"c2": case}
    evaluator_with_cases = RuleEval(cases=cases)
    assert asyncio.run(evaluator_with_cases.score("c2", "REFUSE", _ctx())) == 1.0
    assert asyncio.run(evaluator_with_cases.score("c2", "answer", _ctx())) == 0.0


def test_t05_golden_judge_trajectory() -> None:
    """GoldenJudge：轨迹全等得满分，缺步按比例扣（确定性，不调 LLM）。"""
    judge = GoldenJudge()
    case = EvalCase(case_id="c3", input="q", expected="a",
                    expected_trajectory=("step1", "step2"), should_refuse=False)
    score = asyncio.run(judge.score(case, ["step1", "step2"], _ctx()))
    assert score.value == 1.0
    partial = asyncio.run(judge.score(case, ["step1"], _ctx()))
    assert 0.0 < partial.value < 1.0
