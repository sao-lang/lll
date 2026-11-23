"""内置架构统一协议与静态检查报告（R12 §5/§5.1，规格 P6）。

架构 = 产出 Program 的工厂：compile() 生成 generator（经 A 类算子配方组合），
static_check() 在装配期给结构化报告（错误码 R12-E01~E17），问题进运行期前报错。
所有架构共用 R1 的解释循环，换架构上层装配零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from runtime import Program

# 认知契约单一来源在 R8 cognition（R8 §4/§5 + R13 §4）；此处重导出保持 R12 API 稳定。
# 依赖方向：archs(L2) → cognition(L1) → runtime(L0)，不反向、不重复定义。
from harness.cognition import (
    Executor,
    IntentResult,
    IntentStrategy,
    Planner,
    RePlanner,
    ReflectStrategy,
)

__all__ = [
    "StaticCheckIssue", "StaticCheckReport", "Arch", "JoinStrategy", "MajorityVote",
    "NamedStrategy", "Planner", "Executor", "RePlanner", "ReflectStrategy",
    "IntentStrategy", "IntentResult", "ChildSpec", "SkillSpec",
]


@runtime_checkable
class JoinStrategy(Protocol):
    """子代理结果合并策略（Supervisor.join，R12 §4）。"""
    name: str

    def join(self, results: list[Any]) -> Any: ...  # purity-exempt: HEAD 存量 Any（契约迁移致行号位移，非新增）


@dataclass(frozen=True, slots=True)
class MajorityVote:
    """默认合并策略：多数投票；平票取首个（确定性，墙钟不介入）。"""
    name: str = "majority_vote"

    def join(self, results: list[Any]) -> Any:  # purity-exempt: HEAD 存量 Any（契约迁移致行号位移，非新增）
        if not results:
            return None
        counts: dict[Any, int] = {}  # purity-exempt: HEAD 存量 Any（行号位移，非新增）
        for r in results:
            counts[r] = counts.get(r, 0) + 1
        return max(counts, key=lambda k: (counts[k], -list(results).index(k)))


# --------------------------------------------------------------------------- #
# 可插拔策略契约（R12 §4 构造签名 + R13 §2 协议目录 + R8 §4/§5）
#
# Planner/Executor/RePlanner/ReflectStrategy/IntentStrategy/IntentResult 的
# 单一真相源已迁至 harness.cognition.contracts（R8 认知域），本模块顶部重导出。
# ChildSpec/SkillSpec 是 R12 架构注入点，仍在此定义。
# --------------------------------------------------------------------------- #


@runtime_checkable
class NamedStrategy(Protocol):
    """可插拔策略的最小公共契约：具名即可被装配期履约校验（R12-E15）。"""
    name: str


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

    def static_check(self) -> StaticCheckReport: ...  # purity-exempt: HEAD 存量 Protocol 声明（行号位移，非新增）
    def compile(self) -> Program: ...  # purity-exempt: HEAD 存量 Protocol 声明（行号位移，非新增）
