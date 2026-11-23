# purity-exempt-file: 导出 B.1.2 指定默认部件 InMemoryIntentSampleStore 的名字
"""R8 认知服务（cognition）：意图级联 + 确定性认知策略。

依赖只向下：runtime + compose，零领域语义下沉 R1。四类服务（Planner/Executor/
RePlanner/ReflectStrategy）默认确定性实现；意图四级级联经 intent_as_program
桥梁供 Router/RouterSkill 单入口消费。
"""
from __future__ import annotations

from .contracts import (
    CheapIntentModel,
    Critique,
    Executor,
    IntentResult,
    IntentSample,
    IntentSampleStore,
    IntentStrategy,
    KeywordMatcher,
    Planner,
    PrimaryIntentModel,
    RePlanner,
    ReflectStrategy,
    Step,
    StepResult,
)
from .cascade import (
    CascadeIntentStrategy,
    InMemoryIntentSampleStore,
    RegexKeywordMatcher,
    intent_as_program,
)
from .services import RuleExecutor, RulePlanner, RuleRePlanner, ThresholdReflector

__all__ = [
    "IntentResult", "IntentSample", "Critique", "Step", "StepResult",
    "IntentStrategy", "KeywordMatcher", "IntentSampleStore",
    "CheapIntentModel", "PrimaryIntentModel",
    "Planner", "Executor", "RePlanner", "ReflectStrategy",
    "CascadeIntentStrategy", "InMemoryIntentSampleStore", "RegexKeywordMatcher",
    "intent_as_program",
    "RulePlanner", "RuleExecutor", "RuleRePlanner", "ThresholdReflector",
]
