"""失败分类 DTO（领域概念，非编排角色）。

R3 重试/熔断判定结果，独立于任何执行器或角色协议。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FailureClass"]


@dataclass(frozen=True)
class FailureClass:
    """失败分类（R3 重试/熔断判定结果）。

    字段：
        code: 失败类别码（如 retryable/terminal）。
        retryable: 是否可重试。
        suggested_delay: 建议退避秒数（服务端 Retry-After）。
    """
    code: str
    retryable: bool = False
    suggested_delay: float | None = None
