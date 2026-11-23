"""R8 认知能力包的分支覆盖（真实行为断言，非凑数）。"""
from __future__ import annotations

import pytest

from compose import Registry
from runtime import Terminate

from harness.cognition import CascadeIntentStrategy, IntentResult
from harness.cognition.capability import CognitionCapability
from harness.cognition.reflection_capability import ReflectionCapability


class _WholeStrategy:
    """整体替换策略（只实现 recognize，无 build）——覆盖能力包 recognize 分支。"""
    name = "whole"

    async def recognize(self, text: str, ctx: object) -> IntentResult:
        return IntentResult(label="whole_hit", confidence=0.88, trace=("whole",))


def test_capability_recognize_delegates_to_whole_strategy() -> None:
    cap = CognitionCapability(strategy=_WholeStrategy())  # type: ignore[arg-type]
    result = __import__("asyncio").run(cap.recognize("任意输入"))
    assert result.label == "whole_hit" and result.confidence == 0.88
    assert result.trace == ("whole",)


def test_capability_build_falls_back_to_intent_as_program() -> None:
    """无 build 的策略：build() 走 intent_as_program 桥（真产 Terminate）。"""
    cap = CognitionCapability(strategy=_WholeStrategy())  # type: ignore[arg-type]
    gen = cap.build("输入")
    effect = next(gen)
    assert isinstance(effect, Terminate)
    assert (effect.output or {}).get("label") == "whole_hit"


def test_capability_install_registers_intent_provider() -> None:
    cap = CognitionCapability()
    reg = Registry()
    cap.install(None, reg)
    assert cap is reg.resolve("intent"), "install 注册能力包本身为一等 provider"


def test_reflection_capability_default_reflector_and_install() -> None:
    cap = ReflectionCapability()
    verdict = cap.critique("足够长的答复内容", goal="完整性")
    assert verdict.passed
    reg = Registry()
    cap.install(None, reg)
    assert cap is reg.resolve("reflection")


def test_cognition_capability_default_is_cascade() -> None:
    cap = CognitionCapability()
    assert isinstance(cap.strategy, CascadeIntentStrategy)
