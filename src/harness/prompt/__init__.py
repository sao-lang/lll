"""R11 提示词模板能力：系统/用户双段 + 变量渲染，用户零裸 prompt 注入。

设计：
- PromptTemplate（具名类）：system/user 两段 string.Template（标准库，零新依赖），
  render(**变量) → role/content 消息列表（system 在前、user 在后）。
- PromptCapability 履约 Capability 协议，可 use() 注入；门面/架构从能力取模板，
  不再硬编码 prompt 字符串（默认模板在本模块单一定义，非兼容双轨）。
- 密钥不进模板（安全红线）。
"""
from __future__ import annotations

from dataclasses import dataclass
from string import Template

from runtime import DynamicValue
from compose import Registry

__all__ = ["PromptTemplate", "PromptCapability", "DEFAULT_TEMPLATES"]


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """提示词模板：两段 Template + 变量名渲染（system/user 双段绑定 role）。"""

    name: str
    system: Template
    user: Template

    def render(self, **variables: DynamicValue) -> list[dict[str, DynamicValue]]:
        """渲染为 role/content 消息列表（缺变量由 string.Template 抛 KeyError）。"""
        return [
            {"role": "system", "content": self.system.substitute(**variables)},
            {"role": "user", "content": self.user.substitute(**variables)},
        ]


DEFAULT_TEMPLATES: tuple[PromptTemplate, ...] = (
    PromptTemplate(name="default.system",
                   system=Template("你是${role}助手，回答要${style}。"),
                   user=Template("${input}")),
)


class PromptCapability:
    """提示词能力包：注册模板并按名渲染；门面/架构经它取 prompt。"""

    name = "prompt"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, *, templates: tuple[PromptTemplate, ...] = ()) -> None:
        """构造：显式模板 + 默认模板集（默认仅在无同名显式模板时生效）。"""
        self._templates: dict[str, PromptTemplate] = {}
        for tpl in templates:
            self._templates[tpl.name] = tpl
        for tpl in DEFAULT_TEMPLATES:
            self._templates.setdefault(tpl.name, tpl)

    def register(self, template: PromptTemplate) -> None:
        """注册模板（同名覆盖——显式注入优先于默认）。"""
        self._templates[template.name] = template

    def render(self, name: str, **variables: DynamicValue) -> list[dict[str, DynamicValue]]:
        """按模板名渲染为 role/content 消息；未注册模板 fail-closed 报错。"""
        tpl = self._templates.get(name)
        if tpl is None:
            raise KeyError(f"未注册提示词模板: {name!r}")
        return tpl.render(**variables)

    def install(self, container: object | None = None,  # purity-exempt: container 为 Capability 协议签名（未消费）
                reg: Registry | None = None) -> None:
        """Capability 协议：把模板注册表登记进通用 Registry。"""
        if reg is None or not hasattr(reg, "register"):
            return
        for name, tpl in self._templates.items():
            reg.register(f"prompt:{name}", tpl)  # type: ignore[arg-type]  # purity-exempt: 模板具名 name 履约 Provider
