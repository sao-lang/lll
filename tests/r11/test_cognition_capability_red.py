"""RED：意图识别能力包（CognitionCapability）可随意组装。

三条路径必须都真消费注入的策略：
1. use() 到门面 → agent.recognize_intent() 真调注入策略；
2. 内置 Router 架构 classifier=能力包 → 真调；
3. 组合式 API intent_as_program(能力包) → 真调。
另修两个登记缺口：build() 路径 cheap/primary 注入无效；task/tier 没透传到网关。
"""
from __future__ import annotations

import asyncio

import pytest

from archs import Router
from compose import LLMRequest, LLMResponse, PortCtx, Usage
from harness.cognition import CascadeIntentStrategy, IntentResult


class _Cheap:
    """注入的 L3 便宜模型替身（计数暴露是否真被调用）。"""
    name = "cheap_injected"
    calls = 0

    async def recognize(self, text: str, ctx: object) -> IntentResult:
        self.calls += 1
        return IntentResult(label="cheap_hit", confidence=0.95, trace=("L3",))


class _Primary:
    name = "primary_injected"
    calls = 0

    async def recognize(self, text: str, ctx: object) -> IntentResult:
        self.calls += 1
        return IntentResult(label="primary_hit", confidence=0.9, trace=("L4",))


class _CheapLLM:
    """记录 effect payload 的 LLM 替身（验证 task/tier 透传）。"""
    name = "fake"
    last_usage = None
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.payloads.append({"task_hint": req.task_hint, "meta": dict(ctx.meta)})
        return LLMResponse(content='{"label":"cheap_hit","confidence":0.91}',
                           tool_calls=[], usage=Usage(prompt_tokens=1,
                                                      completion_tokens=1,
                                                      cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_cognition_capability_use_in_facade() -> None:
    """use(CognitionCapability) 后门面 recognize_intent 真调注入策略。"""
    from api import Agent
    from archs import ReAct
    from compose import ToolSpec
    from harness.cognition.capability import CognitionCapability
    from harness.model import ModelCapability

    cheap = _Cheap()
    cap = CognitionCapability(strategy=CascadeIntentStrategy(cheap=cheap))
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=_CheapLLM()))
    agent.use(cap)

    result = agent.recognize_intent("帮我订机票")
    assert result.label == "cheap_hit", f"注入策略未生效：{result}"
    assert cheap.calls >= 1, "注入的 cheap 部件未被调用"


def test_red_cognition_capability_in_router_arch() -> None:
    """内置 Router 架构 classifier=能力包 → 真调注入策略。"""
    from archs import ReAct
    from compose import ToolSpec
    from harness.cognition.capability import CognitionCapability
    from runtime import Runner, Session
    from runtime.types import AgentState

    cheap = _Cheap()
    cap = CognitionCapability(strategy=CascadeIntentStrategy(cheap=cheap))
    arch = Router(classifier=cap, arms={"cheap_hit": ReAct([ToolSpec(name="t")], llm="fake")},
                  default="cheap_hit")

    seen: list[str] = []
    async def call(effect, ctx):  # type: ignore[no-untyped-def]
        from runtime import Invoke, Result
        seen.append(str(effect.port))
        return Result.success({"label": "cheap_hit", "confidence": 0.9})

    result = asyncio.run(Runner(call).run(Session(session_id="router-cap",
                         program_factory=arch.compile, state=AgentState())))
    assert result.status == "done"
    assert cheap.calls >= 1, "Router 未消费注入的能力包"


def test_red_intent_task_tier_reaches_gateway() -> None:
    """L3/L4 的 task/tier 经门面 call → gateway → LLMRequest.task_hint/meta 真透传。"""
    from api import Agent
    from compose import ToolSpec
    from harness.model import ModelCapability
    from archs import ReAct

    llm = _CheapLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))
    agent.say("hi")
    # 直接调用级联 Program 的 Effect 路径（门面 call 消费）
    from harness.cognition.cascade import CascadeIntentStrategy
    from runtime import Runner, Session
    from runtime.types import AgentState
    cascade = CascadeIntentStrategy()  # 无注入 → 走 Invoke L3/L4
    result = asyncio.run(Runner(agent._derive_call(lambda e, c: __import__("runtime").Result.success(None)))
                         .run(Session(session_id="intent-passthrough",
                                      program_factory=cascade.build,
                                      state=AgentState())))
    assert any(p["task_hint"] == "intent" for p in llm.payloads), (
        f"task 未透传到网关：{llm.payloads}")
    assert any(p["meta"].get("tier") in ("cheap", "primary") for p in llm.payloads), (
        f"tier 未透传到网关 meta：{llm.payloads}")
