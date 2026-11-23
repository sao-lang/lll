"""LLM 日志扩展的三种接法（同一次真实调用链上验证）。

回答"用户要封装打印 LLM 日志怎么接"：
① call 装饰器（extra_plugins 变换器）→ 看得到 Effect.payload + Result（全量请求/响应）；
② Lifecycle notice（after_model）→ 只有 phase 级字段（model/error），拿不到正文；
③ LLMPort 包装（协议替换）→ 看得到 LLMRequest/LLMResponse 原文 + usage + stream。

三者都走真实 Runner → ReliabilityExecutor → ModelGateway → fake LLM；fake 只在 tests/。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from compose import (
    Invocation, LifecycleBus, LifecyclePhase, LLMRequest, LLMResponse, PortCtx, Usage,
)
from harness.model import ModelCapability
from harness.resilience import ResilienceCapability
from runtime import Invoke, Result, Runner, Session, Terminate

_MESSAGES = [{"role": "user", "content": "ping"}]


class _FakeLLM:
    """确定性后端替身（仅在 tests/，不进 src）。"""
    name = "fake"
    last_usage: Usage | None = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        self.last_usage = Usage(prompt_tokens=7, completion_tokens=3, cost_usd=0.001)
        return LLMResponse(content="pong", model="m1", usage=self.last_usage)

    async def stream(self, request: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        yield "pong"


def _program():
    resp = yield Invoke(port="llm", payload={"messages": _MESSAGES, "model": "m1"})
    yield Terminate(output=resp.get("content", "?") if isinstance(resp, dict) else "?")
    return None


def _run_with(call) -> object:
    return asyncio.run(Runner(call=call).run(Session(session_id="s-log", program_factory=_program)))


def test_call_decorator_sees_full_request_and_result() -> None:
    """① call 装饰器：Invocation.effect.payload + Result 都拿得到（全量日志的正解）。"""
    entries: list[dict[str, object]] = []

    def logger(nxt):
        async def wrapped(inv: Invocation) -> Result:
            payload = inv.effect.payload if isinstance(inv.effect, Invoke) else {}
            result = await nxt(inv)
            data = result.data if isinstance(result.data, dict) else {}
            entries.append({
                "messages": list(payload.get("messages", [])),
                "model": payload.get("model", ""),
                "content": data.get("content", ""),
                "usage": data.get("usage"),
                "error": result.error.message if result.error else "",
            })
            return result
        return wrapped

    cap = ModelCapability(llm=_FakeLLM())
    executor = ResilienceCapability(extra_plugins=(logger,)).wrap(cap.gateway().as_call())
    out = _run_with(executor.as_call())

    assert getattr(out, "output", None) == "pong"
    assert len(entries) == 1
    assert entries[0]["messages"] == _MESSAGES
    assert entries[0]["model"] == "m1"
    assert entries[0]["content"] == "pong"
    assert isinstance(entries[0]["usage"], Usage)


def test_after_model_notice_carries_no_prompt_or_response() -> None:
    """② Lifecycle notice：只有 model/error，拿不到 messages/content（不能当全量日志用）。"""
    bus = LifecycleBus()
    seen: list[dict[str, object]] = []
    bus.on(LifecyclePhase.AFTER_MODEL, lambda n: seen.append(dict(n.payload)))
    cap = ModelCapability(llm=_FakeLLM(), bus=bus)

    result = asyncio.run(cap.gateway().as_dispatch()(
        Invoke(port="llm", payload={"messages": _MESSAGES, "model": "m1"}),
        PortCtx(session_id="s-log"),
    ))

    assert result.ok
    assert seen == [{"model": "m1", "error": ""}]
    assert "messages" not in seen[0] and "content" not in seen[0]


class _LoggingLLM:
    """③ LLMPort 包装器：协议替换，看到原始请求/响应 + usage。"""
    name = "logging"

    def __init__(self, inner: _FakeLLM) -> None:
        self.inner = inner
        self.entries: list[dict[str, object]] = []

    @property
    def last_usage(self) -> Usage | None:
        return self.inner.last_usage

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        response = await self.inner.complete(request, ctx)
        self.entries.append({
            "messages": list(request.messages),
            "content": response.content,
            "usage": response.usage,
        })
        return response

    async def stream(self, request: LLMRequest, ctx: PortCtx) -> AsyncIterator[str]:
        async for chunk in self.inner.stream(request, ctx):
            yield chunk


def test_llm_port_wrapper_sees_original_request_response_and_usage() -> None:
    """③ Port 包装：能拿到完整 prompt/response/usage，且不改 R1/网关一行。"""
    backend = _LoggingLLM(_FakeLLM())
    cap = ModelCapability(llm=backend)
    executor = ResilienceCapability().wrap(cap.gateway().as_call())

    out = _run_with(executor.as_call())

    assert getattr(out, "output", None) == "pong"
    assert backend.entries[0]["messages"] == _MESSAGES
    assert backend.entries[0]["content"] == "pong"
    assert isinstance(backend.entries[0]["usage"], Usage)


def test_port_wrapper_and_notice_coexist_on_one_call() -> None:
    """推荐组合：Port 包装记正文、notice 记相位，同一次调用两者都触发、互不干扰。"""
    bus = LifecycleBus()
    phases: list[str] = []
    bus.on(LifecyclePhase.BEFORE_MODEL, lambda n: phases.append(n.phase.value))
    bus.on(LifecyclePhase.AFTER_MODEL, lambda n: phases.append(n.phase.value))
    backend = _LoggingLLM(_FakeLLM())
    cap = ModelCapability(llm=backend, bus=bus)
    executor = ResilienceCapability().wrap(cap.gateway().as_call())

    _run_with(executor.as_call())

    assert phases == ["before_model", "after_model"]
    assert len(backend.entries) == 1
