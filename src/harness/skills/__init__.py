"""R5 技能包：磁盘技能发现 + 注入检测（SkillPort 默认实现）。

统一导出注入裁决/协议/默认检测器与 FileSkillProvider；协议契约见 src/hooks/protocols.py::SkillPort。
"""
from __future__ import annotations

from .provider import (
    FileSkillProvider,
    InjectionDetector,
    InjectionVerdict,
    KeywordInjectionDetector,
)

__all__ = [
    "InjectionVerdict",
    "InjectionDetector",
    "KeywordInjectionDetector",
    "FileSkillProvider",
]
