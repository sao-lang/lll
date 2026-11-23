"""RED：ResilienceCapability 真接线（9 策略包装门面 llm dispatch）。

裂缝：use(ResilienceCapability(...)) 进门面后 install 签名不匹配（Registry 被
当 core 传入），返回的 ReliabilityExecutor 无人消费——timeout/retry/breaker/
rate_limit/bulkhead/idempotency/degradation/killswitch/watchdog 全不生效。
修后：门面装配期用 res_cap.wrap(gateway-call-chain) 产出 executor.as_call()
作为 llm dispatch，策略真包装模型调用。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability
from harness.resilience import ResilienceCapability, RetryPolicy, Timeout
from harness.tools import clear_pending


class _FlakyLLM:
    """前 N 次失败、之后成功的 LLM 替身（计数暴露真实调用次数）。"""
    name = "flaky"
    last_usage = None

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("flaky backend down")
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _make_agent(llm: object, resilience: ResilienceCapability | None) -> Agent:
    clear_pending()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))  # type: ignore[arg-type]
    if resilience is not None:
        agent.use(resilience)
    return agent


def test_red_resilience_timeout_truncates_slow_llm() -> None:
    """timeout 策略必须真包装 llm 调用：慢后端被按时截断而非等满 1s。"""

    class _SlowLLM(_FlakyLLM):
        async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
            from compose import LLMResponse, Usage
            self.calls += 1
            await asyncio.sleep(1.0)
            return LLMResponse(content="ok", tool_calls=[],
                               usage=Usage(prompt_tokens=1, completion_tokens=1,
                                           cost_usd=0.0001), model="fake")

    llm = _SlowLLM(0)
    agent = _make_agent(llm, ResilienceCapability(timeout=Timeout(timeout=0.05)))
    start = time.monotonic()
    agent.say("你好")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"timeout 未生效：say 耗时 {elapsed:.2f}s（应被 0.05s 截断）"


def test_red_resilience_retry_recovers_flaky_llm() -> None:
    """retry 策略必须真重试：前 2 次失败的后端在第 3 次成功。"""
    llm = _FlakyLLM(fail_times=2)
    agent = _make_agent(
        llm, ResilienceCapability(retry=RetryPolicy(max_attempts=3, base_delay=0.0)))
    agent.say("你好")
    assert llm.calls == 3, f"retry 未生效：LLM 实调 {llm.calls} 次（应 3 次）"
