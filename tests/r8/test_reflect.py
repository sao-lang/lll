"""R8-T03 反思轮换：确定性 ReflectStrategy + max_rounds 处结束。"""
from __future__ import annotations

import asyncio

from runtime import Result, Runner, Session, Terminate
from archs import Reflection
from harness.cognition import ThresholdReflector


def test_reflector_passes_on_marker() -> None:
    """含 PASS 标记的草稿判通过，score=1。"""
    critique = ThresholdReflector().critique("draft PASS", "goal")
    assert critique.passed and critique.score == 1.0


def test_reflector_rejects_short_draft() -> None:
    """短草稿判不通过，带 comments。"""
    critique = ThresholdReflector(min_len=10).critique("short", "goal")
    assert not critique.passed and critique.comments


def test_reflection_stops_at_max_rounds() -> None:
    """Reflection 架构：critic 永不 passed 时，max_rounds 处结束（防御上限）。"""
    calls = {"n": 0}

    def call(effect, ctx):
        if effect.port == "llm":
            calls["n"] += 1
            if effect.payload.get("purpose") == "critique" or calls["n"] % 2 == 0:
                return Result.success({"passed": False, "score": 0.1})
            return Result.success({"content": f"draft-{calls['n']}"})
        return Result.success(None)

    reflector = ThresholdReflector(min_len=10_000)  # 永不 passed
    arch = Reflection(reflector, max_rounds=2)
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="r8-reflect", program_factory=lambda: arch.compile())))
    assert res.status == "done"
    assert calls["n"] <= 5  # 2 轮 × (draft+critique) + 收尾，防御上限生效
