"""R5 三级披露索引：ToolIndex 把 ToolRegistry 投影为 L1 目录 / L2 schema / L3 正文。

Token 预算是硬约束：超限显式拒绝；合集超过阈值且无命中意图时强制 L1-only。
L3 技能正文需要 SkillPort 参与：注入后按技能清单加载正文（命中注入折叠为空串），
未注入时 l3 留空——协议签名不变，ToolIndex 作为增强实现承载正文加载。
"""
from __future__ import annotations

from hooks import DisclosureIndex, DisclosurePacket, SkillManifest, SkillPort, ToolSpec

from .registry import ToolRegistry

__all__ = ["ToolIndex"]


class ToolIndex:
    """披露索引：DisclosureIndex 的默认实现（name="disclosure"）。"""

    name: str = "disclosure"
    _registry: ToolRegistry
    _skills: SkillPort | None
    _l1_token_budget: int
    _force_threshold: int

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        skills: SkillPort | None = None,
        l1_token_budget: int = 2048,
        force_threshold: int = 20,
    ) -> None:
        """构造索引：注入 registry 与可选 SkillPort，配置 L1 token 预算与强制披露阈值。"""
        self._registry = registry
        self._skills = skills
        self._l1_token_budget = l1_token_budget
        self._force_threshold = force_threshold

    def disclose(self, names: list[str], skills: list[SkillManifest]) -> DisclosurePacket:
        """按命中名与技能清单生成披露包；超预算抛 ValueError。

        参数：
            names: 命中意图的工具名；skills: 技能清单（用于按 name 加载 L3 正文）。
        返回：
            DisclosurePacket（l1 无完整 schema，l2 为命中项的完整 spec，
            l3 在注入 SkillPort 时含技能正文，否则空）。
        """
        l1: tuple[ToolSpec, ...] = tuple(self._registry.catalog(level=1))
        if names:
            # 有命中才展开 L2：整目录过滤，保持注册顺序。
            l2: tuple[ToolSpec, ...] = tuple(
                spec for spec in self._registry.catalog(level=2) if spec.name in names
            )
        else:
            l2 = ()
        # 注入 SkillPort 才加载 L3：按技能清单逐个 name 取正文。
        l3: dict[str, str] = {}
        if self._skills is not None:
            for manifest in skills:
                try:
                    body = self._skills.body(manifest.name)
                except ValueError:
                    # 正文命中注入检测：留空串而非抛穿，避免不可信正文进入披露包。
                    body = ""
                l3[manifest.name] = body
        # 粗略成本：L1 算 name+description，L2 算 schema，L3 计入已加载正文长度。
        token_cost = (
            sum(len(s.name) + len(s.description) for s in l1)
            + sum(len(str(s.parameters_schema)) for s in l2)
            + sum(len(body) for body in l3.values())
        )
        total = len(self._registry.catalog())
        if total > self._force_threshold and not names:
            # 合集过大且无命中意图：强制 L1-only，L2 保持空（不报错、不全量注入）。
            l2 = ()
        if token_cost > self._l1_token_budget:
            # 预算超限必须显式拒绝，避免静默把超量上下文塞给模型。
            raise ValueError(
                f"Disclosure token budget exceeded: {token_cost} > {self._l1_token_budget}"
            )
        return DisclosurePacket(l1=l1, l2=l2, l3=l3, token_cost=token_cost)
