"""R8-T01/T08 意图识别与四级级联（TDD RED → GREEN）。

验证：逐级短路 / 兜底 / 四部件注入 / strategy 整体替换 / trace 观察字段 /
Router 单入口消费（裂缝修复）。用真实 Runner 驱动 Program，call 为本地确定性回注。
"""
from __future__ import annotations

import asyncio

from harness.cognition import (
    CascadeIntentStrategy,
    IntentResult,
    IntentSample,
    KeywordMatcher,
    intent_as_program,
)
from harness.cognition.cascade import InMemoryIntentSampleStore, RegexKeywordMatcher


def test_t01_intent_result_fields() -> None:
    """R8-T01：IntentResult 带 label/confidence/gaps，trace 可选。"""
    r = IntentResult(label="chat", confidence=0.9, gaps=(), trace=("L1:chat:0.9",))
    assert r.label == "chat" and r.confidence == 0.9 and r.trace == ("L1:chat:0.9",)


def test_t08_l1_short_circuit_no_llm() -> None:
    """R8-T08：L1 关键词命中达阈值即停，不下发任何 LLM Invoke（短路在 generator 层）。"""
    from runtime import Invoke, Terminate

    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({"weather": "weather"}))
    gen = intent_as_program(strategy, text="what is the weather")()
    effects = []
    effect = next(gen)
    while True:
        if isinstance(effect, Terminate):
            break
        effects.append(effect)
        effect = gen.send(None)
    assert effects == [], f"L1 命中不应有后续 Effect，实际 {effects}"
    # output 通过 Terminate 传出（桥接器形态）
    assert isinstance(effect, Terminate)
    out = effect.output
    assert out["label"] == "weather" and out["hit_stage"] == "L1"


def test_t08_l2_semantic_short_circuit() -> None:
    """R8-T08：L1 未中、L2 采样库语义命中即停。"""
    from runtime import Invoke, Terminate

    samples = InMemoryIntentSampleStore((
        IntentSample(label="refund", text="i want a refund please", embedding=()),
    ))
    strategy = CascadeIntentStrategy(
        keyword=RegexKeywordMatcher({}),
        samples=samples,
        thresholds={"L2": 0.5},
    )
    gen = intent_as_program(strategy, text="i want a refund")()
    effect = next(gen)
    effects = []
    while not isinstance(effect, Terminate):
        effects.append(effect)
        effect = gen.send(None)
    assert effects == []
    assert effect.output["label"] == "refund"
    assert effect.output["hit_stage"] == "L2"


def test_t08_l3_cheap_model_via_invoke() -> None:
    """R8-T08：L1/L2 未中，L3 便宜模型经 Invoke(port=llm) 命中，短路在 generator 层。"""
    from runtime import Invoke, Terminate

    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({}))
    gen = intent_as_program(strategy, text="unrecognized input")()
    effect = next(gen)
    assert isinstance(effect, Invoke) and effect.port == "llm"
    effect = gen.send({"label": "billing", "confidence": 0.85})
    assert isinstance(effect, Terminate)
    assert effect.output["label"] == "billing"
    assert effect.output["hit_stage"] == "L3"
    assert len(effect.output["trace"]) == 3  # L1/L2 未中 + L3 命中


def test_t08_l4_primary_fallback() -> None:
    """R8-T08：L3 未达阈值，进 L4 主模型兜底。"""
    from runtime import Invoke, Terminate

    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({}))
    gen = intent_as_program(strategy, text="vague")()
    e1 = next(gen)
    assert isinstance(e1, Invoke)
    e2 = gen.send({"label": "unknown", "confidence": 0.2})  # L3 未达阈值
    assert isinstance(e2, Invoke)
    e3 = gen.send({"label": "primary-hit", "confidence": 0.95})  # L4 命中
    assert isinstance(e3, Terminate)
    assert e3.output["label"] == "primary-hit"
    assert e3.output["hit_stage"] == "L4"


def test_t08_all_miss_default() -> None:
    """R8-T08：四级全不满足 → default/unknown 兜底。"""
    from runtime import Invoke, Terminate

    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({}))
    gen = intent_as_program(strategy, text="gibberish")()
    e = next(gen)
    e = gen.send({"label": "l3", "confidence": 0.1})
    e = gen.send({"label": "l4", "confidence": 0.1})
    assert isinstance(e, Terminate)
    assert e.output["label"] == "unknown"
    assert e.output["hit_stage"] == "default"


def test_t08_component_injection() -> None:
    """R8-T08：四部件注入——自定义 L1 生效，且 trace 记录部件 name。"""
    from runtime import Terminate

    custom = RegexKeywordMatcher({"urgent": "escalate"})
    strategy = CascadeIntentStrategy(keyword=custom)
    gen = intent_as_program(strategy, text="urgent please")()
    e = next(gen)
    while not isinstance(e, Terminate):
        e = gen.send(None)
    assert e.output["label"] == "escalate"
    assert any(t.startswith("L1[") for t in e.output["trace"])


def test_t08_strategy_override() -> None:
    """R8-T08：strategy 整体替换——注入的 IntentStrategy 不被级联包装。"""
    from runtime import Terminate

    class Custom:
        name = "custom"

        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="custom-label", confidence=1.0)

    gen = intent_as_program(Custom(), text="anything")()
    e = next(gen)
    while not isinstance(e, Terminate):
        e = gen.send(None)
    assert e.output["label"] == "custom-label"
    assert e.output["hit_stage"] == "strategy"


def test_t08_router_single_entry() -> None:
    """R8-T06/T08：Router 经 intent_as_program 单入口消费真实分类器结果（非 default 兜底）。"""
    from runtime import Result, Runner, Session
    from archs import Router

    class Arm:
        def __init__(self, label: str) -> None:
            self.label = label

        def compile(self):
            def p():
                from runtime import Terminate
                yield Terminate(output=f"arm-{self.label}")
            return p()

    # 真实返回 IntentResult 的 Callable 分类器；default 指向另一臂，
    # 若桥接失效会走 default=fallback 而非命中臂，断言能暴露裂缝。
    def classifier(text: str) -> IntentResult:
        return IntentResult(label="weather", confidence=0.9)

    arch = Router(classifier, {"weather": Arm("weather"), "fallback": Arm("fallback")},
                  default="fallback")
    res = asyncio.run(Runner(call=lambda e, c: Result.success(None)).run(
        Session(session_id="r8-router", program_factory=lambda: arch.compile())))
    assert res.status == "done"
    assert res.output == "arm-weather", f"Router 应走分类器命中的 arm，实际 {res.output!r}"
