"""R5 能力装配：ToolsCapability 把 registry/mcp/skills/disclosure 组装成默认能力包。

缺席即构造内置默认实现（规则 9）：registry 缺则用 R9 三 Port 建 ToolRegistry，
disclosure 缺则用该 registry 建 ToolIndex；mcp/skills 可缺席为 None。
"""
from __future__ import annotations

from compose.tool_layer import layer_program
from runtime import Program

from compose import (
    AuditPort,
    DisclosureIndex,
    DisclosurePacket,
    HumanPort,
    Registry,
    SandboxPort,
    SkillManifest,
    SkillPort,
)

from .index import ToolIndex
from .mcp import MCPManager
from .registry import ToolRegistry

__all__ = ["ToolsCapability"]

# R2 规范端口名（与 PORT_PROTOCOLS 键对齐）：install 据此把各 port 注册进 Registry。
_PORT_TOOL = "tool"
_PORT_DISCLOSURE = "disclosure"
_PORT_MCP = "mcp"
_PORT_SKILL = "skill"


class ToolsCapability:
    """工具能力包：持有默认注册表/披露索引与可替换的 MCP、技能实现。

    属性：
        name/version/requires: R13 Capability 三要素。
    """

    name = "tools"
    version = "5.0.0"
    requires: tuple[str, ...] = ()

    _registry: ToolRegistry
    _mcp: MCPManager | None
    _skills: SkillPort | None
    _disclosure: DisclosureIndex

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        mcp: MCPManager | None = None,
        skills: SkillPort | None = None,
        disclosure: DisclosureIndex | None = None,
        sandbox: SandboxPort | None = None,
        human: HumanPort | None = None,
        audit: AuditPort | None = None,
    ) -> None:
        """装配能力：registry/disclosure 缺席时建默认实现，mcp/skills 可缺席。"""
        if registry is None:
            # 规则 9：协议可注入则必须有开箱默认实现，三 Port 透传给默认注册表。
            self._registry = ToolRegistry(sandbox=sandbox, human=human, audit=audit)
        else:
            # 自定义注册表已自带路由依赖，忽略外层 sandbox/human/audit 避免语义歧义。
            self._registry = registry
        self._mcp = mcp
        self._skills = skills
        if disclosure is None:
            # 规则 9：披露策略可替换，但默认必须可用；透传 skills 以启用 L3 正文加载。
            self._disclosure = ToolIndex(self._registry, skills=self._skills)
        else:
            self._disclosure = disclosure

    def build(self) -> Program:
        """ToolLayer 外壳（规格 P5）：产出"用工具"的 Program 工厂（内部不改）。"""
        return layer_program("tool", payload={"layer": "tools"})()

    def install(self, container: object, reg: Registry) -> None:  # purity-exempt: Capability 协议签名 container 必填，本能力只消费 reg
        """把持有的 port 注册进共用 Provider 注册表（对齐 Capability 两参协议）。

        参数：
            container: Capability 协议要求传入的装配容器，本能力不消费。
            reg: 共用 Provider 注册表；按 port 名注册 tool/disclosure/mcp/skill。
        """
        # 先装配期自动注册 @tool 待注册项，再注册各 Port；
        # 为何在前：flush 可能抛重名/freeze，须在 Port 注册前暴露，避免半装配。
        self._registry.register_pending()
        # reg 提供 register(name, provider) 现成 API，直接注册持有的四个 port。
        reg.register(_PORT_TOOL, self._registry)
        reg.register(_PORT_DISCLOSURE, self._disclosure)
        if self._mcp is not None:
            # MCPPort 缺席为 None，跳过注册（缺席即降级）。
            reg.register(_PORT_MCP, self._mcp)
        if self._skills is not None:
            # SkillPort 缺席为 None，跳过注册（缺席即降级）。
            reg.register(_PORT_SKILL, self._skills)

    def registry(self) -> ToolRegistry:
        """返回本能力持有的工具注册表。"""
        return self._registry

    def index(self) -> DisclosureIndex:
        """返回本能力持有的披露索引。"""
        return self._disclosure

    def manager(self) -> MCPManager | None:
        """返回 MCP 管理器；未装配时为 None。"""
        return self._mcp

    def skills(self) -> SkillPort | None:
        """返回技能 Port；未装配时为 None。"""
        return self._skills

    def disclose(self, names: list[str]) -> DisclosurePacket:
        """委托披露索引生成披露包，技能清单从 SkillPort.catalog() 取。"""
        manifests: list[SkillManifest] = []
        if self._skills is not None:
            # 技能缺席时传空清单，披露仍能按工具维度降级工作。
            manifests = list(self._skills.catalog())
        return self._disclosure.disclose(names, manifests)
