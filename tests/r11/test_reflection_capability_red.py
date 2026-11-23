"""RED：反思能力包（ReflectionCapability）可随意组装。

- use(ReflectionCapability(reflector=...)) 后门面 reflective_run 真调注入 reflector；
- 内置 Reflection 架构构造参数=能力包 → critique(draft, goal, ctx) 三参真传；
- 组合式 API：loop_until(critique→revise) 由能力包提供 critique 决策。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import AgentState, Invoke, Result, Runner, Session, Terminate

from harness.cognition import Critique, ThresholdReflector


class _SpyReflector:
    name = "spy_reflector"
    calls: list[tuple[str, str, object]] = []

    def critique(self, draft: str, goal: str, ctx: object = None) -> Critique:
        self.calls.append((draft, goal, ctx))
        return Critique(passed=True, score=1.0, comments="injected")


def test_red_reflection_capability_use_in_facade() -> None:
    """use() 后门面 reflective_run 真调注入 reflector（含 ctx 第三参）。"""
    from api import Agent
    from archs import ReAct
    from compose import ToolSpec
    from harness.cognition.reflection_capability import ReflectionCapability
    from harness.model import ModelCapability

    reflector = _SpyReflector()
    cap = ReflectionCapability(reflector=reflector)
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(cap)

    result = agent.evaluate("草稿", goal="写得更好")
    assert result.passed and result.comments == "injected"
    assert reflector.calls and reflector.calls[0][0] == "草稿"
    assert reflector.calls[0][1] == "写得更好"
    assert reflector.calls[0][2] is not None, "ctx 第三参未传"


def test_red_reflection_arch_uses_capability_reflector() -> None:
    """Reflection 架构 reflector=能力包 → 循环里真调注入策略。"""
    from archs import Reflection
    from harness.cognition.reflection_capability import ReflectionCapability

    reflector = _SpyReflector()
    cap = ReflectionCapability(reflector=reflector)
    arch = Reflection(reflector=cap, max_rounds=2)

    async def call(effect, ctx):  # type: ignore[no-untyped-def]
        from runtime import Invoke, Result
        if isinstance(effect, Invoke) and effect.port == "llm":
            return Result.success({"content": "修订稿"})
        return Result.success(None)

    res = asyncio.run(Runner(call).run(Session(
        session_id="reflect-cap", program_factory=arch.compile, state=AgentState())))
    assert res.status == "done"
    assert reflector.calls, "Reflection 架构未消费注入的能力包"


class _LLM:
    name = "fake"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError
