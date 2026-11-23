"""RED：门面 say() 注入的 SecurityCapability 必须真进执行链（全模块体检 B5）。

现状 agent.py:401-404 调 assemble_runtime 不传 security/approval/audit，
导致用户注入的 sandbox/human/audit 被 runtime.py:169 的默认新建替换。
本测试注入带 spy 的 SecurityCapability，用真实 ReAct + FakeLLM 触发 CRITICAL 工具，
断言门面装配出的 tools 用的是注入实例且 spy 真被调用。
"""
from __future__ import annotations

from typing import Any

from api import Agent
from archs import ReAct
from compose import DangerLevel, LLMRequest, PortCtx, ToolCall, ToolSpec, LLMResponse, Usage
from harness.security import ConsoleHumanPort, LocalSandbox, SecurityCapability


class _SpySandbox(LocalSandbox):
    """spy：标记是否被调用（fake 仅在 tests/）。"""
    def __init__(self) -> None:
        super().__init__(allow_sync=True)
        self.calls = 0

    async def run(self, fn, args, ctx):  # type: ignore[no-untyped-def]
        self.calls += 1
        return await super().run(fn, args, ctx)


class _ToolLLM:
    """FakeLLM：首轮发 CRITICAL 工具调用，次轮给结论（fake 仅在 tests/）。"""
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> Any:
        self.n += 1
        if self.n == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(call_id="c1", name="danger_tool", args={})],
                usage=Usage(prompt_tokens=10, completion_tokens=2, cost_usd=0.0001),
                model="fake",
            )
        return LLMResponse(
            content="done", tool_calls=[],
            usage=Usage(prompt_tokens=10, completion_tokens=2, cost_usd=0.0001),
            model="fake",
        )

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_facade_uses_injected_security_sandbox() -> None:
    """门面注入的 spy sandbox 必须成为执行链里的 sandbox（当前 FAIL）。"""
    spy = _SpySandbox()
    human = ConsoleHumanPort(answerer=lambda q, o: "approve")
    sec = SecurityCapability(sandbox=spy, human=human)

    tool_spec = ToolSpec(name="danger_tool", description="danger", danger=DangerLevel.CRITICAL)
    agent = Agent(ReAct([tool_spec], llm="fake", max_turns=3))
    agent.capability(sec)
    from harness.model import ModelCapability
    agent.capability(ModelCapability(llm=_ToolLLM()))
    agent.register_tool("danger_tool", lambda: "boom", danger=DangerLevel.CRITICAL)

    agent.say("go")

    assert agent.runtime is not None and agent.runtime.tools is not None
    # 注入的 spy 必须真进执行链；当前门面不传 security → runtime 默认新建 → FAIL
    assert agent.runtime.tools._sandbox is spy, (
        "门面注入的 SecurityCapability.sandbox 必须真进执行链，"
        f"实得 {type(agent.runtime.tools._sandbox).__name__}"
    )
    assert spy.calls == 1, f"CRITICAL 工具应走注入的 spy sandbox 1 次，实得 {spy.calls}"
