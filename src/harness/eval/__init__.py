"""R10 评测（eval）：EvalPort→RuleEval（确定性）+ Judge→GoldenJudge/LLMJudge。

两层评测正交：
- 确定性层（不调 LLM）：RuleEval 断言评分 + GoldenJudge 黄金轨迹匹配——CI 回归门禁；
- 语义层（调 LLM）：LLMJudge 通用大模型评审器，rubric/LLM/量程全经构造参数注入，
  经 EvalCapability(evaluator=...) 替换注入，fail-closed 兜底。
"""
from __future__ import annotations

from .types import EvalCase, Score, SuiteReport
from .rules import GoldenJudge, RuleEval
from .llm_judge import LLMJudge
from .capability import EvalCapability

__all__ = ["EvalCase", "Score", "SuiteReport", "RuleEval", "GoldenJudge", "LLMJudge", "EvalCapability"]
