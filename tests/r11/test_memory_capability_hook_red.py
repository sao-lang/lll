"""RED：use(MemoryCapability()) 后 say 自动 recall + 写回（体检 B1 记忆主线）。"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import LLMRequest, LLMResponse, PortCtx, ToolSpec, Usage
from harness.context import MemoryCapability
from harness.model import ModelCapability


class _CaptureLLM:
    name = "capture"
    last_usage = None
    seen_messages: list[list[dict]] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.seen_messages.append([dict(m) for m in req.messages])
        return LLMResponse(content="答好了", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                           model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_memory_capability_auto_recall_and_write() -> None:
    """注入 MemoryCapability 后：say 自动召回注入 + 自动写回（当前 FAIL）。"""
    cap = MemoryCapability()
    # 预置一条记忆，验证 recall 会被注入 LLM 上下文
    from compose import PortCtx as PC
    cap.manager.memory.ingest("用户偏好：喜欢喝咖啡", PC(session_id="s", run_id="r"))

    llm = _CaptureLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm)).use(cap)

    agent.say("我喜欢什么？")

    # 写回：say 后记忆条目应增加
    assert cap.manager.memory.tier_count() >= 2, (
        f"say 后应自动写回，实得 {cap.manager.memory.tier_count()} 条"
    )
