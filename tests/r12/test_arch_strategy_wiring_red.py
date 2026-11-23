"""RED：注入到架构的策略，其方法必须被真实调用（全模块体检 B2）。

现状 PlanExecute 只读 planner.prompt、executor 完全不调、replanner 结果丢弃；
Reflection 只读 reflector.prompt。注入 spy 策略，跑真实 Runner，断言方法被调用。
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import PlanExecute, Reflection
from compose import LLMRequest, PortCtx, LLMResponse, Usage
from harness.cognition import Critique, Step, StepResult
from harness.model import ModelCapability
from runtime import Result, Terminate


class _SpyPlanner:
    name = "spy_planner"
    prompt = "plan"

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, goal: str, ctx=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return (Step(action="a1", step_id="s0"),)


class _SpyExecutor:
    name = "spy_executor"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, step: Step, ctx=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return StepResult(step_id=step.step_id, output="ok", ok=True)


class _SpyReflector:
    name = "spy_reflector"
    prompt = "critique"

    def __init__(self) -> None:
        self.calls = 0

    def critique(self, draft: str, goal: str, ctx=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return Critique(passed=True, score=1.0, comments="ok")


class _EchoLLM:
    name = "fake"
    last_usage = None

    async def complete(self, req: LLMRequest, ctx: PortCtx):  # type: ignore[no-untyped-def]
        return LLMResponse(content="draft text", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                           model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_planexecute_calls_injected_planner() -> None:
    """PlanExecute 必须真调注入的 planner.plan()（当前 FAIL）。"""
    planner = _SpyPlanner()
    executor = _SpyExecutor()
    agent = Agent(PlanExecute(planner, executor))
    agent.capability(ModelCapability(llm=_EchoLLM()))

    agent.say("do a1")

    assert planner.calls >= 1, f"注入的 planner.plan() 应被调用，实得 {planner.calls}"


def test_red_reflection_calls_injected_reflector() -> None:
    """Reflection 必须真调注入的 reflector.critique()（当前 FAIL）。"""
    reflector = _SpyReflector()
    agent = Agent(Reflection(reflector))
    agent.capability(ModelCapability(llm=_EchoLLM()))

    agent.say("draft something")

    assert reflector.calls >= 1, f"注入的 reflector.critique() 应被调用，实得 {reflector.calls}"
