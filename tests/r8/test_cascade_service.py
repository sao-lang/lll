"""R8 级联 async 路径 + 策略注入 + 终态兜底（补齐真实行为覆盖）。"""
from __future__ import annotations

import asyncio

from runtime import Terminate
from harness.cognition import (
    CascadeIntentStrategy,
    IntentResult,
    RegexKeywordMatcher,
    intent_as_program,
)


def _drive(gen):
    """驱动 Program 到 Terminate，沿途回注 None（供无 Effect 的策略）。"""
    effect = next(gen)
    while not isinstance(effect, Terminate):
        effect = gen.send(None)
    return effect.output


def test_async_recognize_short_circuit_l1() -> None:
    """recognize() async 路径：L1 命中即停，不调后续模型。"""
    class CountingModel:
        name = "counting"
        calls = 0

        async def recognize(self, text: str, ctx: object) -> IntentResult:
            type(self).calls += 1
            return IntentResult(label="model", confidence=0.9)

    strategy = CascadeIntentStrategy(
        keyword=RegexKeywordMatcher({"hello": "greeting"}),
        cheap=CountingModel(), primary=CountingModel(),
    )
    result = asyncio.run(strategy.recognize("hello there", None))
    assert result.label == "greeting"
    assert CountingModel.calls == 0  # L1 短路，模型零调用


def test_async_recognize_uses_injected_cheap() -> None:
    """recognize()：L1/L2 未中时用注入的 L3 便宜模型。"""
    class Cheap:
        name = "cheap"
        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="cheap-hit", confidence=0.85)

    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({}), cheap=Cheap())
    result = asyncio.run(strategy.recognize("unknown", None))
    assert result.label == "cheap-hit"
    assert "L3" in "".join(result.trace)


def test_async_recognize_l3_fail_l4_hit() -> None:
    """recognize()：L3 未达阈值 → await L4 主模型兜底命中。"""
    class Low:
        name = "low"
        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="low", confidence=0.1)

    class High:
        name = "high"
        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="high", confidence=0.95)

    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({}),
                                     cheap=Low(), primary=High())
    result = asyncio.run(strategy.recognize("x", None))
    assert result.label == "high"
    assert "L3" in "".join(result.trace) and "L4" in "".join(result.trace)


def test_program_path_ignores_injected_model_and_uses_effect() -> None:
    """Program 路径统一走 Invoke Effect（即便注入 recognize 模型，也不直接 IO）。"""
    class Cheap:
        name = "cheap"
        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="should-not-be-called", confidence=1.0)

    from runtime import Invoke
    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({}), cheap=Cheap())
    gen = strategy.build("x")
    effect = next(gen)
    assert isinstance(effect, Invoke) and effect.port == "llm"
    gen.close()


def test_intent_as_program_none_fallback() -> None:
    """strategy=None 产 unknown 兜底（不抛、不空转）。"""
    out = _drive(intent_as_program(None, text="x")())
    assert out["label"] == "unknown" and out["hit_stage"] == "default"


def test_intent_as_program_callable_coroutine() -> None:
    """Callable 分类器返回 coroutine 时适配（R12 旧路径兼容）。"""
    async def classifier(text: str) -> IntentResult:
        return IntentResult(label="async-callable", confidence=0.9)

    out = _drive(intent_as_program(classifier, text="x")())
    assert out["label"] == "async-callable"
