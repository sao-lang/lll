# purity-exempt-file: Protocol 契约声明：方法体省略号与 ctx 参数是接口签名必需，非占位实现
"""R8 认知域契约（DTO + 协议）单一真相源。

设计依据：R8 §4/§5 + R13 §4/§4.1。archs/base.py 从本模块重导出
IntentResult/IntentStrategy/Planner/Executor/RePlanner/ReflectStrategy，
避免两处定义分裂（单一来源，接线而非兼容层）。

原则：DTO 不可变（frozen+slots）；协议用 @runtime_checkable 供装配期履约校验；
方法签名只声明规格明确给出的，不编造空头契约挡住实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from runtime import AgentState, DynamicValue

__all__ = [
    "IntentResult",
    "IntentSample",
    "Critique",
    "Step",
    "StepResult",
    "IntentStrategy",
    "KeywordMatcher",
    "IntentSampleStore",
    "CheapIntentModel",
    "PrimaryIntentModel",
    "Planner",
    "Executor",
    "RePlanner",
    "ReflectStrategy",
]


# --------------------------------------------------------------------------- #
# DTO（R8 §5 元数据）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IntentResult:
    """意图识别结果：label/confidence/gaps + 可选 trace（观察语义）。

    字段：
        label: 命中标签（Router arms 的 key）。
        confidence: 0~1 置信度，低于阈值走 default。
        gaps: 信息缺口，触发澄清（R8 §5）。
        trace: 逐级观察记录（stage:label:confidence），不下沉 R1 内核。
    """
    label: str
    confidence: float
    gaps: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntentSample:
    """L2 语义匹配采样：label + 样本文本 + 可选嵌入（规则 3 动态边界）。"""
    label: str
    text: str
    embedding: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class Critique:
    """反思评判结果（R8 §5）：passed/score/comments。"""
    passed: bool
    score: float = 0.0
    comments: str = ""


@dataclass(frozen=True, slots=True)
class Step:
    """可执行步骤（R8 §5）：明确动作 + 校验条件。"""
    action: str
    check: str = ""
    step_id: str = ""


@dataclass(frozen=True, slots=True)
class StepResult:
    """步骤执行结果：step_id + output + ok。"""
    step_id: str
    output: DynamicValue = None
    ok: bool = True


# --------------------------------------------------------------------------- #
# 策略协议（R8 §4 / R13 §4）
# --------------------------------------------------------------------------- #
class NamedStrategy(Protocol):
    """可插拔策略最小契约：具名即可被装配期履约校验（R12-E15）。"""
    name: str


@runtime_checkable
class IntentStrategy(NamedStrategy, Protocol):
    """整体意图策略：注入即整包替换默认级联。"""
    async def recognize(self, text: str, ctx: AgentState | None) -> IntentResult: ...


@runtime_checkable
class KeywordMatcher(NamedStrategy, Protocol):
    """L1 关键词匹配（零成本）。"""
    def match(self, text: str, ctx: AgentState | None) -> IntentResult | None: ...


@runtime_checkable
class IntentSampleStore(NamedStrategy, Protocol):
    """L2 意图采样库数据源（相似度算法由框架内置）。"""
    def search(self, text: str, top_k: int = 5) -> tuple[IntentSample, ...]: ...


@runtime_checkable
class CheapIntentModel(NamedStrategy, Protocol):
    """L3 便宜模型判断。"""
    async def recognize(self, text: str, ctx: AgentState | None) -> IntentResult: ...


@runtime_checkable
class PrimaryIntentModel(NamedStrategy, Protocol):
    """L4 主模型判断（兜底）。"""
    async def recognize(self, text: str, ctx: AgentState | None) -> IntentResult: ...


@runtime_checkable
class Planner(NamedStrategy, Protocol):
    """规划策略：把目标分解为步骤。"""
    pass


@runtime_checkable
class Executor(NamedStrategy, Protocol):
    """步骤执行策略。"""
    pass


@runtime_checkable
class RePlanner(NamedStrategy, Protocol):
    """重规划策略：失败后产出新步骤。"""
    pass


@runtime_checkable
class ReflectStrategy(NamedStrategy, Protocol):
    """反思评判策略。"""
    pass
