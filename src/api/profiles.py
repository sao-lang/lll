"""R11 profile 预设：dev/production/regulated 三档能力清单（R11 §5）。

只声明"装什么能力"，不实现能力——能力实现在各模块（R3/R5/R6/R7/R8/R9/R10），
本文件是装配层的具名预设表（规则 3 动态边界：profile 名由 dict 键承载）。
"""
from __future__ import annotations

__all__ = ["PROFILES", "profile_names"]

# 每档的能力名清单（装配期按名 install 对应 Capability）。
PROFILES: dict[str, tuple[str, ...]] = {
    "dev": ("resilience",),
    "production": ("resilience", "rag", "security", "observability"),
    "regulated": ("resilience", "rag", "security", "observability", "audit"),
}


def profile_names() -> tuple[str, ...]:
    """返回已登记 profile 名（不可变顺序快照）。"""
    return tuple(PROFILES)
