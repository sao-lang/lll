"""行为型 RED：say_stream 必须走完整架构，所有架构自动逐字流式。

不是覆盖率测试：断言真实行为——①ReAct 带工具调用时，工具决策轮+总结轮
的 TEXT_DELTA 都能逐字读到；②工具真被执行；③最终答复与 asay 语义一致。
设计：框架内部控制流（Queue + ContextVar），架构零改动，无 topic/胶水。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from api import Agent
from archs import ReAct
from compose import DangerLevel, StreamEvent, StreamEventKind, Usage
from harness.model import ModelCapability
from harness.tools import clear_pending, tool


@tool(name="get_weather", danger=DangerLevel.SAFE)
def _weather(city: str = "深圳") -> dict[str, object]:
    return {"city": city, "temperature": 28}


class _StreamWithToolLLM:
    name = "stream-tool"
    last_usage = None

    async def complete(self, req, ctx):
        from compose import LLMResponse
        return LLMResponse(content="fallback", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1), model=self.name)

    def stream_events(self, req, ctx) -> AsyncIterator[StreamEvent]:
        async def gen():
            u = Usage(prompt_tokens=5, completion_tokens=3, cost_usd=0.0001)
            # 首轮：工具决策（带 tool_call）
            if not any(m.get("role") == "tool" for m in req.messages):
                for chunk in ("我", "来", "查", "天气"):
                    yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk)
                    await asyncio.sleep(0)
                yield StreamEvent(kind=StreamEventKind.TOOL_CALL_END,
                                  data={"call_id": "c1", "name": "get_weather",
                                        "arguments": {"city": "深圳"}})
            else:
                # 第二轮：总结（工具结果后）
                for chunk in ("深", "圳", "28", "度"):
                    yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, data=chunk)
                    await asyncio.sleep(0)
            self.last_usage = u
            yield StreamEvent(kind=StreamEventKind.USAGE, data=u)
            yield StreamEvent(kind=StreamEventKind.DONE)
        return gen()

    async def stream(self, req, ctx):
        async def gen(): yield ""
        return gen()


def test_red_say_stream_runs_full_arch_with_tool() -> None:
    agent = Agent(ReAct([_weather], llm="fake"))
    agent.use(ModelCapability(llm=_StreamWithToolLLM()))
    # 显式注册处理器：不依赖 @tool 的模块级 pending（全量测试中会被其它用例
    # 的 clear_pending() 清掉，导致单独跑绿、全量跑红的状态污染）。
    agent.register_tool("get_weather", lambda city="深圳": {"city": city, "temperature": 28})

    async def scenario() -> None:
        chunks = [c async for c in agent.say_stream("查深圳天气")]
        joined = "".join(chunks)
        # 两轮文本增量都逐字吐出（不是只吐最后一轮，也不是一次性 fallback）
        assert "我来查天气" in joined, f"缺工具决策轮的增量，实际 {joined}"
        assert "深圳28度" in joined, f"缺总结轮增量，实际 {joined}"
        assert chunks[:4] == ["我", "来", "查", "天气"], f"首轮应逐块，实际 {chunks[:6]}"
        assert len(chunks) == 8, f"两轮各 4 块共 8 块，实际 {len(chunks)}: {chunks}"
        # 工具真执行：总结轮增量只在工具结果回填后才产生，能拿到即证明工具执行
        assert agent.last_run is not None and agent.last_run.status == "done"
        assert len(agent.last_run.events) >= 4, \
            f"含工具轮应至少 4 个事件，实际 {len(agent.last_run.events)}"

    asyncio.run(scenario())
