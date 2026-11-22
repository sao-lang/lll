"""内置架构统一协议与静态检查报告（R12 §5/§5.1，规格 P6）。

架构 = 产出 Program 的工厂：compile() 生成 generator（经 A 类算子配方组合），
static_check() 在装配期给结构化报告（错误码 R12-E01~E17），问题进运行期前报错。
所有架构共用 R1 的解释循环，换架构上层装配零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from runtime import AgentState, Program

__all__ = [
    "StaticCheckIssue", "StaticCheckReport", "Arch", "JoinStrategy", "MajorityVote",
    "NamedStrategy", "Planner", "Executor", "RePlanner", "ReflectStrategy",
    "IntentStrategy", "IntentResult", "ChildSpec", "SkillSpec",
]


@runtime_checkable
class JoinStrategy(Protocol):
    """子代理结果合并策略（Supervisor.join，R12 §4）。"""
    name: str

    def join(self, results: list[Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class MajorityVote:
    """默认合并策略：多数投票；平票取首个（确定性，墙钟不介入）。"""
    name: str = "majority_vote"

    def join(self, results: list[Any]) -> Any:
        if not results:
            return None
        counts: dict[Any, int] = {}
        for r in results:
            counts[r] = counts.get(r, 0) + 1
        return max(counts, key=lambda k: (counts[k], -list(results).index(k)))


# --------------------------------------------------------------------------- #
# 可插拔策略契约（R12 §4 构造签名 + R13 §2 协议目录 + R8 §4/§5）
#
# 规格用 Planner/Executor/RePlanner/ReflectStrategy/IntentStrategy/ChildSpec/
# SkillSpec 命名这些注入点，但旧实现全写成裸 Any。这里把协议名落到类型层：
# 每个策略的装配期契约是「具名（name 非空）」——static_check 据此判 E15
# 「策略未履约：缺 name」；方法签名只在规格明确给出时声明（IntentStrategy），
# 其余不编造规格未定的方法，避免空头契约挡住未来实现。
# --------------------------------------------------------------------------- #


@runtime_checkable
class NamedStrategy(Protocol):
    """可插拔策略的最小公共契约：具名即可被装配期履约校验（R12-E15）。

    字段：
        name: 策略注册名；static_check 只读此字段判「策略未履约」。
    """
    name: str


@runtime_checkable
class Planner(NamedStrategy, Protocol):
    """规划策略（PlanExecute/Supervisor 注入点，R13 §5）。

    把任务分解为可执行步骤；分解逻辑由实现方持有，架构只消费产出的步骤。
    """


@runtime_checkable
class Executor(NamedStrategy, Protocol):
    """步骤执行策略（PlanExecute 注入点，R13 §5）。

    执行单个已规划步骤；执行细节由实现方持有。
    """


@runtime_checkable
class RePlanner(NamedStrategy, Protocol):
    """重规划策略（PlanExecute 可选注入点，R13 §5）。

    失败或环境变化时产出新步骤序列。
    """


@runtime_checkable
class ReflectStrategy(NamedStrategy, Protocol):
    """反思评判策略（Reflection 注入点，R13 §5）。

    对草稿产出评判（passed/score/comments，见 R8 §5 Critique）。
    """


@dataclass(frozen=True, slots=True)
class IntentResult:
    """意图识别结果（R8 §5）：单入口 `IntentStrategy.recognize` 的返回信封。

    字段：
        label: 命中的意图标签（对应 Router.arms 的 key）。
        confidence: 置信度（0~1），低于 min_confidence 走 default。
        gaps: 信息缺口，触发澄清（R8 §5）。
        trace: 观察语义的命中级/逐级 label+confidence，非内核必填。
    """
    label: str
    confidence: float
    gaps: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()


@runtime_checkable
class IntentStrategy(NamedStrategy, Protocol):
    """意图识别整体策略（Router/RouterSkill 注入点，R13 §2 注入点①）。

    传入即整包替换默认四级级联（CascadeIntentStrategy，见 R13 §4.1）。
    Router 只消费单次 recognize 结果，对级联内部级数/阈值无感。
    """
    async def recognize(self, text: str, ctx: AgentState) -> IntentResult: ...


@runtime_checkable
class ChildSpec(NamedStrategy, Protocol):
    """子 Agent 规格（Supervisor 注入点，R12 §4）。

    声明子 Agent 类型；并发上限由 max_children 承载、预算由 Budgets 分账，
    二者都不在架构 generator 内自数（铁律）。
    """


@runtime_checkable
class SkillSpec(NamedStrategy, Protocol):
    """技能规格（RouterSkill 路由目标之一，R12 §4）。

    name 为技能名，正文（part:body）加载由 R5 SkillPort 履约，架构不实现注册。
    """


@dataclass(frozen=True, slots=True)
class StaticCheckIssue:
    code: str
    message: str
    node: str | None = None


@dataclass(frozen=True, slots=True)
class StaticCheckReport:
    errors: tuple[StaticCheckIssue, ...] = ()
    arch: str = ""
    version: str = ""
    ports_required: tuple[str, ...] = ()
    budgets_resolved: dict[str, int | float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


@runtime_checkable
class Arch(Protocol):
    name: str
    version: str

    def static_check(self) -> StaticCheckReport: ...
    def compile(self) -> Program: ...
