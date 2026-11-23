"""RED：Agent(system=...) 一等参数必须把 system 注入首轮 LLM 消息（体检 B9）。

现状门面无 system 参数，demo 靠用户侧 _SystemPromptLLM 包装器补（hack）。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import LLMRequest, LLMResponse, PortCtx, ToolSpec, Usage
from harness.model import ModelCapability


class _CaptureLLM:
    name = "capture"
    last_usage = None
    seen: list[list[dict]] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.seen.append([dict(m) for m in req.messages])
        return LLMResponse(content="done", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                           model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_system_param_injected_into_first_llm_call() -> None:
    """Agent(system=...) 后首轮 LLM messages 应含 system 消息（当前 FAIL）。"""
    llm = _CaptureLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), system="你是简洁的中文助手")
    agent.capability(ModelCapability(llm=llm))

    agent.say("查天气")

    assert llm.seen, "LLM 应被调用"
    first = llm.seen[0]
    assert any(m.get("role") == "system" and "中文助手" in str(m.get("content"))
               for m in first), f"首轮应含 system 消息，实得 {first!r}"
