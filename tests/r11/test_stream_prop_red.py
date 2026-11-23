"""RED：agent.stream 公开可观测属性（体检 B10）。

当前只有私有 _stream 和 agent.runtime.stream；用户目标写法要直接 agent.stream。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from harness.observability import InMemoryEventStream
from compose import ToolSpec


def test_red_agent_stream_public_property() -> None:
    """agent.stream 应暴露事件流底座（当前 FAIL：无此属性）。"""
    stream = InMemoryEventStream()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), stream=stream)
    assert agent.stream is stream, "agent.stream 应返回注入的事件流底座"
