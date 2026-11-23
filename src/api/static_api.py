# purity-exempt-file: static_check_api 的 arch 为协议形状（compose/api 不反向依赖 archs）
"""R11 装配期静态检查聚合（不执行 Program，只汇总 Arch 报告）。"""
from __future__ import annotations

from archs.base import StaticCheckReport

__all__ = ["static_check_api"]


def static_check_api(arch: object) -> StaticCheckReport:
    """聚合架构的 static_check 报告（无报告时空 ok）。"""
    check = getattr(arch, "static_check", None)
    if callable(check):
        report = check()
        if isinstance(report, StaticCheckReport):
            return report
    return StaticCheckReport(arch=getattr(arch, "name", ""),
                             version=getattr(arch, "version", ""))
