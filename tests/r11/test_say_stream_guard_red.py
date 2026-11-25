"""RED：say_stream 必须与 asay 一样经过注入护栏，不得 fail-open 放行恶意输入。

裂缝：asay 在入口调 guardrail.check（deny/ask 阻断、redact 脱敏），但 say_stream
直接把 text 塞进 drive()→_arun()，绕过护栏——注入 SecurityCapability 后，恶意
文本经流式入口直达 LLM（P3b 复现：LLM 被触达、guard 收到=[]）。
修后：say_stream 走同一护栏口径，deny 时 yield [blocked] 且不触达 LLM。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from api import Agent
from archs import ReAct
from compose import LLMResponse, ToolSpec, Usage
from harness.model import ModelCapability
from harness.security import SecurityCapability


class DenyGuard:
    name = "deny-all"
    direction = "input"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def check(self, data, ctx):
        self.seen.append(str(data))
        return type("V", (), {
            "action": "deny", "rule": "always", "redacted": str(data)})()


class Echo:
    name = "echo"
    last_usage = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req, ctx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="REACHED-LLM", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1),
                           model=self.name)

    async def stream(self, req, ctx):
        self.calls += 1

        async def gen():
            yield "REACHED-LLM"
        return gen()


def test_red_say_stream_honors_guardrail() -> None:
    async def run() -> None:
        guard = DenyGuard()
        llm = Echo()
        agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), session_id="s")
        agent.use(ModelCapability(llm=llm))
        agent.use(SecurityCapability(guardrail=guard))

        chunks = [c async for c in agent.say_stream("malicious-input")]

        assert guard.seen == ["malicious-input"], \
            f"流式入口也应过护栏，实际 guard 收到 {guard.seen}"
        assert llm.calls == 0, "护栏 deny 后不得触达 LLM"
        assert "".join(chunks).startswith("[blocked]"), \
            f"应 yield 拒绝提示，实际 {chunks}"

    asyncio.run(run())
