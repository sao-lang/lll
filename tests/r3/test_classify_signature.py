"""签名对齐：FailureClassifier.classify 的 ctx 是鸭子类型 object。

分类器只依赖 result.error，
ctx 放宽为 object（可传 RequestScope / 任意上下文）。
本测试用类型提示锁定签名，防止契约漂移。
"""
from __future__ import annotations

from typing import get_type_hints

from runtime import Result

from harness.resilience import FailureClassifier, classify_failure
from harness.resilience.types import _DefaultClassifier


def test_classify_signature_ctx_is_object() -> None:
    """协议 / 默认实现 / 统一入口三处 ctx 注解都必须是 object。"""
    proto = get_type_hints(FailureClassifier.classify)
    assert proto["ctx"] is object, f"协议 ctx={proto['ctx']}，应为 object"
    assert proto["result"] is Result
    default = get_type_hints(_DefaultClassifier.classify)
    assert default["ctx"] is object, f"默认实现 ctx={default['ctx']}，应为 object"
    entry = get_type_hints(classify_failure)
    assert entry["ctx"] is object, f"入口 ctx={entry['ctx']}，应为 object"


def test_default_classifier_is_structural_implementation() -> None:
    """默认实现满足 FailureClassifier 协议（结构型，无需显式继承）。"""
    assert isinstance(_DefaultClassifier(), FailureClassifier)
