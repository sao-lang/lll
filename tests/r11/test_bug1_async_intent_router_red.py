"""RED-Bug1: 异步 IntentStrategy（仅 recognize）在 Runner 活事件循环内必须可路由。

审计 P8：intent_as_program 的 recognize 适配分支用 asyncio.run 推进协程，
Router 在 Runner 的活 loop 内 compile → RuntimeError + 协程泄漏。
正确修法：复用同文件 _await_sync（无 loop 直接 run，有 loop 走独立线程）。
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import ReAct, Router
from compose import LLMResponse, ToolSpec, Usage
from harness.cognition import IntentResult, intent_as_program
from harness.model import ModelCapability
from runtime import Terminate


class _AsyncOnlyStrategy:
    name = "async_only"

    def __init__(self, label: str = "a") -> None:
        self.label = label
        self.called = 0

    async def recognize(self, text: str, ctx: object) -> IntentResult:
        self.called += 1
        return IntentResult(label=self.label, confidence=0.9)


class _AnswerLLM:
    name = "answer"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[object] = []

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        self.calls.append(req)
        return LLMResponse(content=self.answer, usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.001), model="fake")

    async def stream(self, req: object, ctx: object) -> object:
        raise NotImplementedError


def test_red_router_async_intent_strategy_e2e() -> None:
    """Router + 异步策略端到端：answer 必须是 arm 的真实答复，status=done。"""
    strategy = _AsyncOnlyStrategy()
    llm = _AnswerLLM("ROUTED-OK")
    agent = Agent(
        Router(strategy, {"a": ReAct([ToolSpec(name="t")], llm="fake")}, default="a"),
        session_id="bug1-e2e",
    )
    agent.use(ModelCapability(llm=llm))

    answer = agent.say("route me")

    assert answer == "ROUTED-OK", f"路由后应出 arm 的真实答复，实得 {answer!r}"
    assert agent.last_run is not None and agent.last_run.status == "done", (
        f"status={getattr(agent.last_run, 'status', None)!r} "
        f"error={getattr(agent.last_run, 'error', None)!r}")
    assert strategy.called == 1, f"异步 recognize 体必须真执行，called={strategy.called}"
    assert len(llm.calls) == 1, f"arm 应真调一次 LLM，实得 {len(llm.calls)}"


def test_red_async_recognize_program_in_running_loop() -> None:
    """活 loop 内推进仅 recognize 策略的 Program：产 Terminate，无协程泄漏。"""
    strategy = _AsyncOnlyStrategy("b")
    program = intent_as_program(strategy, text="hi")()

    async def drive() -> object:
        return next(program)  # type: ignore[arg-type]

    effect = asyncio.run(drive())

    assert isinstance(effect, Terminate), f"应产 Terminate，实得 {type(effect).__name__}"
    assert effect.output["label"] == "b", f"label 错：{effect.output!r}"
    assert strategy.called == 1, f"recognize 必须恰好 await 一次，called={strategy.called}"


def test_red_async_callable_strategy_program_in_running_loop() -> None:
    """纯 callable 返回 awaitable 的分支同样不能在活 loop 内 asyncio.run。"""
    called = 0

    async def recognize(text: str) -> IntentResult:
        nonlocal called
        called += 1
        return IntentResult(label="c", confidence=0.8)

    program = intent_as_program(recognize, text="hi")()

    async def drive() -> object:
        return next(program)  # type: ignore[arg-type]

    effect = asyncio.run(drive())

    assert isinstance(effect, Terminate), f"应产 Terminate，实得 {type(effect).__name__}"
    assert effect.output["label"] == "c", f"label 错：{effect.output!r}"
    assert called == 1, f"异步 callable 必须恰好 await 一次，called={called}"
