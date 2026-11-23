"""门面可观测注入口测试（demo 用门面重写的前提，TDD RED）。"""
from __future__ import annotations

import asyncio
from typing import Any

from api import Agent
from archs import ReAct
from compose import LLMResponse, ToolSpec, Usage
from harness.model import ModelCapability
from harness.observability import InMemoryEventStream
from runtime import AgentState, Budgets, InMemoryEventStore


class _LLM:
    name = "fake"
    last_usage: Usage | None = None

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(), model="fake")

    async def stream(self, r: object, c: object) -> object:  # pragma: no cover
        raise NotImplementedError


def test_facade_accepts_stream_store_budgets_and_exposes_last_run() -> None:
    """门面可选注入 stream/store/budgets，并暴露 last_run 供可观测读取。"""
    stream = InMemoryEventStream()
    store = InMemoryEventStore()
    budgets = Budgets(max_turns=3, max_tokens=100, max_cost_usd=1.0)

    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2),
                  session_id="obs-session", stream=stream, store=store, budgets=budgets)
    agent.capability(ModelCapability(llm=_LLM()))
    assert agent.say("hi") == "ok"

    # 注入的底座被真用（同一实例），且 last_run 可读预算
    assert agent.runtime is not None
    assert agent.runtime.stream is stream
    assert agent.last_run is not None
    assert agent.last_run.session_id == "obs-session"
    assert agent.last_run.output is not None


def test_facade_defaults_unchanged() -> None:
    """默认路径仍零胶水：不注入底座也能 say，last_run 可读但底座自建。"""
    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    agent.capability(ModelCapability(llm=_LLM()))
    assert agent.say("hi") == "ok"
    assert agent.last_run is not None
    assert agent.runtime is not None


def test_facade_budgets_config_reaches_run_state() -> None:
    """注入的 budgets 配置值真进 run.state（R1 fold 产生新对象，配置值保持）。"""
    budgets = Budgets(max_turns=7, max_tokens=999, max_cost_usd=3.0)
    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2),
                  budgets=budgets)
    agent.capability(ModelCapability(llm=_LLM()))
    agent.say("hi")
    run_budgets = agent.last_run.state.budgets
    assert (run_budgets.max_turns, run_budgets.max_tokens, run_budgets.max_cost_usd) == (7, 999, 3.0)
