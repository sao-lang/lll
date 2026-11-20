"""内置架构统一协议与静态检查报告（R12 §5/§5.1，规格 P6）。

架构 = 产出 Program 的工厂：compile() 生成 generator（经 A 类算子配方组合），
static_check() 在装配期给结构化报告（错误码 R12-E01~E17），问题进运行期前报错。
所有架构共用 R1 的解释循环，换架构上层装配零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from runtime import Program

__all__ = ["StaticCheckIssue", "StaticCheckReport", "Arch", "JoinStrategy", "MajorityVote"]


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
