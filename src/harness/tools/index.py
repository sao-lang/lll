"""ToolIndex：三级渐进式披露（L1 目录 / L2 完整 schema / L3 技能正文）。

披露成本进 trace（ToolIndexEntry.token_cost）；逻辑计数而非墙钟，保证确定性。
工具数 >20 强制启用；<10 可用 ToolRegistry(index_enabled=False) 降级为全量注入。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast

from runtime import DynamicValue, canonical

from .registry import ToolRegistry

__all__ = ["ToolIndexEntry", "SkillProvider", "DisclosurePacket", "ToolIndex"]


class DisclosurePacket(TypedDict):
    """一级渐进式披露的完整结果包；四个键均由 ToolIndex 固定产出。"""

    L1: list[DynamicValue]  # 常驻目录（name + 一句话）
    L2: list[dict[str, DynamicValue]]  # 按需加载的工具 schema
    L3: dict[str, str]  # 技能名 → 正文（技能名由用户决定，故键开放）
    token_cost: int  # 累计披露 token 成本


@dataclass(frozen=True)
class ToolIndexEntry:
    """一次披露的记录；用于统计成本与审计（逻辑计数，无墙钟）。"""

    name: str  # 被披露的工具 / 技能名
    level: int          # 1 / 2 / 3
    loaded_at: int      # 逻辑计数（第几次披露），不是墙钟
    token_cost: int  # 估算 token 成本


class SkillProvider(Protocol):
    """技能正文提供者（M11 DisclosureIndex 的最小形态）。"""

    def body(self, name: str) -> str:
        """按名字返回技能正文。"""
        ...


def _tokens(data: DynamicValue) -> int:
    """按规范化 JSON 长度粗估 token 数（4 字符 ≈ 1 token），至少为 1。"""
    return max(1, len(canonical(data)) // 4)


class ToolIndex:
    """三级渐进式披露索引：L1 目录 / L2 schema / L3 技能正文。"""

    def __init__(
        self,
        registry: ToolRegistry,
        skill_provider: SkillProvider | None = None,
        *,
        threshold: int = 20,
        l1_token_budget: int = 2000,
        l2_token_budget: int = 8000,
    ) -> None:
        """装配披露策略：threshold 触发阈值，两级预算防止上下文膨胀。"""
        self.registry = registry  # 工具注册表
        self.skill_provider = skill_provider  # L3 技能正文来源
        self.threshold = threshold  # 工具数超过该值才需渐进披露
        self.l1_token_budget = l1_token_budget  # L1 目录 token 上限
        self.l2_token_budget = l2_token_budget  # 单个 L2 schema token 上限
        self.entries: list[ToolIndexEntry] = []  # 披露记录
        self._counter = 0  # 逻辑披露计数器

    def _record(self, name: str, level: int, data: DynamicValue) -> int:
        """记录一次披露，返回其估算 token 成本。"""
        self._counter += 1
        cost = _tokens(data)
        self.entries.append(ToolIndexEntry(name, level, self._counter, cost))
        return cost

    def should_index(self) -> bool:
        """工具数是否达到强制渐进披露的阈值。"""
        return len(self.registry.catalog()) >= self.threshold

    def catalog(self) -> list[DynamicValue]:
        """L1：常驻上下文的全量目录（只有 name + 一句话）。"""
        items = self.registry.catalog(level=1)
        cost = self._record("*", 1, items)
        if cost > self.l1_token_budget:
            raise ValueError(f"L1 catalog exceeds token budget: {cost} > {self.l1_token_budget}")
        return items

    def load_schema(self, names: Iterable[str]) -> list[dict[str, DynamicValue]]:
        """L2：被选中的工具才加载完整参数 schema。"""
        schemas = []
        for name in names:
            schema = self.registry.schema_of(name)
            cost = self._record(name, 2, schema)
            if cost > self.l2_token_budget:
                raise ValueError(f"L2 schema {name} exceeds token budget: {cost}")
            schemas.append({"name": name, "parameters_schema": schema})
        return schemas

    def load_body(self, skill: str) -> str:
        """L3：技能正文（RouterSkill 的 Effect(skill, part=body) 落地形态）。"""
        if self.skill_provider is None:
            raise ValueError("no SkillProvider configured for L3 disclosure")
        body = self.skill_provider.body(skill)
        self._record(skill, 3, body)
        return body

    def disclose(self, names: Iterable[str] = (), skills: Iterable[str] = ()) -> DisclosurePacket:
        """一次完整披露包：L1 恒给，L2/L3 按需；返回 token 统计供 trace。"""
        l1 = self.catalog()
        l2 = self.load_schema(names)
        l3 = {s: self.load_body(s) for s in skills}
        return cast(DisclosurePacket, {
            "L1": l1,
            "L2": l2,
            "L3": l3,
            "token_cost": sum(e.token_cost for e in self.entries),
        })
