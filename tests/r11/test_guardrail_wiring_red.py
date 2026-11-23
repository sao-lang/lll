"""RED：SecurityCapability.guardrail 接入门面输入链。

裂缝：SecurityCapability 存了 guardrail 字段但全 src 0 消费点——say() 输入
从不经过护栏，deny 裁决无人执行（死注入）。修后：asay 前置 guardrail.check，
deny 时 fail-closed 返回拒绝语义、不调 LLM、不写记忆。
"""
from __future__ import annotations

import pytest

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability
from harness.security import SecurityCapability, SecurityCtx


class _CountingLLM:
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        self.calls += 1
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _DenyAll:
    """测试护栏替身：一律 deny（记录是否真被门面调用）。"""
    name = "deny_all"
    checked: list[str] = []

    def check(self, value: str, ctx: SecurityCtx) -> object:
        self.checked.append(value)
        from harness.security import SafetyVerdict
        return SafetyVerdict(action="deny", rule="deny_all", reason="测试拒绝")


def test_red_guardrail_deny_blocks_llm_call() -> None:
    """deny 裁决必须真阻断：不调 LLM、返回拒绝语义。"""
    llm = _CountingLLM()
    guard = _DenyAll()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm)).use(SecurityCapability(guardrail=guard))  # type: ignore[arg-type]

    answer = agent.say("随便说点什么")

    assert guard.checked, "guardrail.check 未被门面调用（死注入）"
    assert llm.calls == 0, f"deny 后不应调 LLM，实调 {llm.calls} 次"
    assert answer, "deny 后应返回拒绝语义文本"


def test_red_guardrail_allow_passes_through() -> None:
    """allow 裁决放行：LLM 正常调用。"""

    class _AllowAll(_DenyAll):
        def check(self, value: str, ctx: SecurityCtx) -> object:  # noqa: F811
            self.checked.append(value)
            from harness.security import SafetyVerdict
            return SafetyVerdict(action="allow", rule="allow_all", reason="放行")

    llm = _CountingLLM()
    guard = _AllowAll()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm)).use(SecurityCapability(guardrail=guard))  # type: ignore[arg-type]

    agent.say("你好")

    assert guard.checked, "guardrail.check 未被门面调用"
    assert llm.calls == 1, f"allow 后应调 LLM 一次，实调 {llm.calls} 次"
