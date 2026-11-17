"""R3 设计 §4 签名对齐测试：FailureClassifier.classify 的 ctx 必须是 HookCtx。

背景：曾出现设计写 HookCtx、代码写 object 的漂移。本测试用类型提示锁定签名，
防止再次漂回 object（不改行为，只锁契约）。
"""
from __future__ import annotations

from typing import get_type_hints

from hooks import HookCtx
from runtime import Result

from harness.resilience import FailureClassifier, classify_failure
from harness.resilience.types import _DefaultClassifier


def test_classify_signature_ctx_is_hookctx() -> None:
    """协议 / 默认实现 / 统一入口三处 ctx 注解都必须是 HookCtx。"""
    # 协议
    proto = get_type_hints(FailureClassifier.classify)
    assert proto["ctx"] is HookCtx, f"协议 ctx={proto['ctx']}，应为 HookCtx"
    assert proto["result"] is Result
    # 默认实现
    default = get_type_hints(_DefaultClassifier.classify)
    assert default["ctx"] is HookCtx, f"默认实现 ctx={default['ctx']}，应为 HookCtx"
    # 统一入口函数
    entry = get_type_hints(classify_failure)
    assert entry["ctx"] is HookCtx, f"入口 ctx={entry['ctx']}，应为 HookCtx"


def test_default_classifier_is_structural_implementation() -> None:
    """默认实现满足 FailureClassifier 协议（结构型，无需显式继承）。"""
    assert isinstance(_DefaultClassifier(), FailureClassifier)
