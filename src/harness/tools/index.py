"""ToolIndex：三级渐进式披露（L1 目录 / L2 完整 schema / L3 技能正文）。

披露成本进 trace（ToolIndexEntry.token_cost）；逻辑计数而非墙钟，保证确定性。
工具数 >20 强制启用；<10 可用 ToolRegistry(index_enabled=False) 降级为全量注入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from runtime import canonical

from .registry import ToolRegistry

__all__ = ["ToolIndexEntry", "SkillProvider", "ToolIndex"]


@dataclass(frozen=True)
class ToolIndexEntry:
    name: str
    level: int          # 1 / 2 / 3
    loaded_at: int      # 逻辑计数（第几次披露），不是墙钟
    token_cost: int


class SkillProvider(Protocol):
    def body(self, name: str) -> str: ...


def _tokens(data: Any) -> int:
    return max(1, len(canonical(data)) // 4)


class ToolIndex:
    def __init__(
        self,
        registry: ToolRegistry,
        skill_provider: SkillProvider | None = None,
        *,
        threshold: int = 20,
        l1_token_budget: int = 2000,
        l2_token_budget: int = 8000,
    ) -> None:
        self.registry = registry
        self.skill_provider = skill_provider
        self.threshold = threshold
        self.l1_token_budget = l1_token_budget
        self.l2_token_budget = l2_token_budget
        self.entries: list[ToolIndexEntry] = []
        self._counter = 0

    def _record(self, name: str, level: int, data: Any) -> int:
        self._counter += 1
        cost = _tokens(data)
        self.entries.append(ToolIndexEntry(name, level, self._counter, cost))
        return cost

    def should_index(self) -> bool:
        return len(self.registry.catalog()) >= self.threshold

    def catalog(self) -> list[Any]:
        """L1：常驻上下文的全量目录（只有 name + 一句话）。"""
        items = self.registry.catalog(level=1)
        cost = self._record("*", 1, items)
        if cost > self.l1_token_budget:
            raise ValueError(f"L1 catalog exceeds token budget: {cost} > {self.l1_token_budget}")
        return items

    def load_schema(self, names: Iterable[str]) -> list[dict[str, Any]]:
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

    def disclose(self, names: Iterable[str] = (), skills: Iterable[str] = ()) -> dict[str, Any]:
        """一次完整披露包：L1 恒给，L2/L3 按需；返回 token 统计供 trace。"""
        l1 = self.catalog()
        l2 = self.load_schema(names)
        l3 = {s: self.load_body(s) for s in skills}
        return {
            "L1": l1,
            "L2": l2,
            "L3": l3,
            "token_cost": sum(e.token_cost for e in self.entries),
        }
