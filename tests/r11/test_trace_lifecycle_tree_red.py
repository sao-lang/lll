"""RED：链路树——16 相位 + llm/tool span 必须进同一棵树（Jaeger 可见走向）。

裂缝：当前 llm/tool span 平级（parent_id 空），相位走 PhaseOTLPBridge 胶水层，
Jaeger 上是散点而非"输入→相位→LLM→工具→输出"的树。修后：框架 Trace 原生
订阅 LifecycleBus，每轮 say 产 root span，相位+llm/tool 全挂其下；OTLP 导出后
平台直接显示完整链路，无需 examples 胶水层。R1 零改动。
"""
from __future__ import annotations

import pytest

from api import Agent
from archs import ReAct
from compose import DangerLevel
from harness.model import ModelCapability
from harness.observability import NoopTelemetry
from harness.tools import clear_pending, tool


@pytest.fixture(autouse=True)
def _isolated_pending_tools() -> None:
    clear_pending()


class _ToolLLM:
    name = "fake"
    last_usage = None
    _called = False

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, ToolCall, Usage
        if not self._called:
            self._called = True
            return LLMResponse(content="", tool_calls=[ToolCall(
                call_id="c1", name="get_weather", args={"city": "深圳"})],
                usage=Usage(prompt_tokens=1, completion_tokens=1), model="fake")
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=2, completion_tokens=2), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@tool(name="get_weather", danger=DangerLevel.SAFE)
def _weather(city: str = "深圳") -> dict[str, object]:
    return {"city": city, "temperature": 28}


def test_red_trace_has_root_and_phase_spans_in_one_tree() -> None:
    """say 后 trace 有：root span + 相位 span + llm/tool span，全部同 trace_id、成树。"""
    agent = Agent(ReAct([_weather], llm="fake"))
    agent.use(ModelCapability(llm=_ToolLLM()))

    agent.say("查深圳天气")

    spans = agent.trace.spans()
    names = [s.name for s in spans]
    components = {s.component for s in spans}
    # root span：整轮会话的根，所有节点挂其下
    roots = [s for s in spans if not s.parent_id]
    assert len(roots) == 1, f"应有唯一 root span，实际 roots={[r.name for r in roots]}"
    root_id = roots[0].span_id
    # 16 相位至少覆盖关键几个（session_start/before_model/after_tool/session_end）
    assert any(n.startswith("phase:") for n in names), f"缺相位 span，实际 {names}"
    assert any("session_start" in n for n in names), f"缺 session_start 相位，实际 {names}"
    # turn:N 容器挂 root 下，llm/tool/相位挂 turn 下（多轮同树不同分支）
    turns = [s for s in spans if s.name.startswith("turn:")]
    assert len(turns) == 1, f"一轮应有一个 turn 容器，实际 {[s.name for s in turns]}"
    turn_id = turns[0].span_id
    assert turns[0].parent_id == root_id, "turn 必须挂 session root"
    llm_spans = [s for s in spans if s.component == "llm"]
    tool_spans = [s for s in spans if s.component == "tool"]
    assert llm_spans and tool_spans, f"缺 llm/tool span：llm={len(llm_spans)} tool={len(tool_spans)}"
    assert all(s.parent_id == turn_id for s in llm_spans + tool_spans), \
        f"llm/tool 必须挂 turn 下，实际 parents={[s.parent_id for s in llm_spans + tool_spans]}"
    phase_spans = [s for s in spans if s.name.startswith("phase:")]
    assert all(s.parent_id == turn_id for s in phase_spans), \
        f"相位 span 必须挂 turn 下，实际 parents={[s.parent_id for s in phase_spans]}"
    # 全部同 trace_id
    assert all(s.trace_id == agent.trace.trace_id for s in spans)


def test_red_trace_implements_bus_plugin_protocol() -> None:
    """Trace 必须实现 LifecycleBus 插件协议：subscribe 后 emit 真产出链路 span。"""
    from harness.observability import Trace
    from compose import LifecycleBus, LifecyclePhase, Notice
    trace = Trace(trace_id="t-plugin")
    assert trace.name == "trace.lifecycle", f"插件名精确，实际 {trace.name}"
    bus = LifecycleBus()
    returned = bus.subscribe(trace)  # 原生插件注册，不靠 examples 胶水
    assert returned is bus
    # 行为断言：emit session_start 真产出 session root + turn + 相位 span
    bus.emit(Notice(phase=LifecyclePhase.SESSION_START, session_id="t-plugin"))
    names = [s.name for s in trace.spans()]
    assert "session" in names and "turn:1" in names and "phase:session_start" in names, \
        f"插件订阅后应产出链路树，实际 {names}"


def test_red_multi_turn_is_one_tree_with_turn_nodes() -> None:
    """多轮 say 应是一棵树：唯一 session root，每轮一个 turn 子节点。"""
    agent = Agent(ReAct([_weather], llm="fake"))
    agent.use(ModelCapability(llm=_ToolLLM()))

    agent.say("查天气")
    agent.say("再查一次")

    spans = agent.trace.spans()
    roots = [s for s in spans if not s.parent_id]
    assert len(roots) == 1, f"多轮应共享唯一 root，实际 {[r.name for r in roots]}"
    root_id = roots[0].span_id
    turns = [s for s in spans if s.name.startswith("turn:")]
    assert len(turns) == 2, f"两轮应有两个 turn 节点，实际 {[s.name for s in turns]}"
    assert all(s.parent_id == root_id for s in turns), "turn 应挂 session root"
    # 相位与 llm/tool 不应直接挂 root（应挂 turn）
    direct = [s.name for s in spans if s.parent_id == root_id and not s.name.startswith("turn:")]
    assert not direct, f"相位/llm/tool 应挂 turn，不应直接挂 root：{direct}"
