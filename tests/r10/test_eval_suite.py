"""R10 回归套件 + 无注册用例兜底（补齐真实行为覆盖）。"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.eval import EvalCase, GoldenJudge, RuleEval


def _ctx() -> PortCtx:
    return PortCtx(session_id="s10", run_id="r10")


def test_regression_suite_report() -> None:
    """regression 用录制回放的 actual 评分；未采集的用例 fail-closed 记 0。"""
    evaluator = RuleEval(cases={
        "c1": EvalCase(case_id="c1", input="q", expected="a", actual="a"),  # 通过
        "c2": EvalCase(case_id="c2", input="q", expected="b", actual="WRONG"),  # 失败
        "c3": EvalCase(case_id="c3", input="q", expected="c"),  # 未采集 → 0
    })
    report = asyncio.run(evaluator.regression("golden-v1", _ctx()))
    assert report.suite == "golden-v1"
    assert report.total == 3 and report.passed == 1
    assert abs(report.average - (1 / 3)) < 1e-9


def test_score_tuple_fallback() -> None:
    """无注册用例时按 (expected, actual) 二元组确定性比较。"""
    evaluator = RuleEval()
    assert asyncio.run(evaluator.score("unknown", ("a", "a"), _ctx())) == 1.0
    assert asyncio.run(evaluator.score("unknown", ("a", "b"), _ctx())) == 0.0
    assert asyncio.run(evaluator.score("unknown", "scalar", _ctx())) == 0.0


def test_golden_judge_mismatch_and_empty() -> None:
    """GoldenJudge：完全不同的轨迹得 0；无期望轨迹但给实际轨迹得 0。"""
    judge = GoldenJudge()
    case = EvalCase(case_id="c", input="q", expected="a",
                    expected_trajectory=("x", "y"))
    assert asyncio.run(judge.score(case, ["p", "q"], _ctx())).value == 0.0
    empty_expected = EvalCase(case_id="c2", input="q", expected="a")
    assert asyncio.run(judge.score(empty_expected, ["p"], _ctx())).value == 0.0
