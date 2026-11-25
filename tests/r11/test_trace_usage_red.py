"""RED：LLM span 必须记录真实 usage（tokens/cost），不能全是 0。

裂缝：门面 _call_llm 拿到 Result.usage（R1 Event 也记了 tokens），但从未写进
TraceSpan.tokens/cost，页面与 Jaeger OTLP 上全显示 0 tok / $0.000000。
修后：每次 LLM 调用结束，当前 llm span 带上该次 usage 的 token 与成本。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, Usage
from harness.model import ModelCapability
from harness.tools import clear_pending, tool


class _UsageLLM:
    name = "fake"
    last_usage = None
    _called = False

    async def complete(self, req, ctx):
        from compose import LLMResponse, Usage
        u = Usage(prompt_tokens=17, completion_tokens=5, cost_usd=0.0012)
        type(self).last_usage = u
        if not self._called:
            self._called = True
            return LLMResponse(content="", tool_calls=[], usage=u, model=self.name)
        return LLMResponse(content="ok", tool_calls=[], usage=u, model=self.name)

    async def stream(self, req, ctx):
        raise NotImplementedError


def test_red_llm_span_has_real_tokens_and_cost() -> None:
    clear_pending()
    agent = Agent(ReAct([], llm="fake"))
    agent.use(ModelCapability(llm=_UsageLLM()))
    agent.say("hi")
    llm_spans = [s for s in agent.trace.spans() if s.component == "llm"]
    assert llm_spans, "应有 llm span"
    assert all(s.tokens == 22 for s in llm_spans), \
        f"llm span 应记 prompt+completion=22 tokens，实际 {[s.tokens for s in llm_spans]}"
    assert all(abs(s.cost - 0.0012) < 1e-9 for s in llm_spans), \
        f"llm span 应记 cost=0.0012，实际 {[s.cost for s in llm_spans]}"
