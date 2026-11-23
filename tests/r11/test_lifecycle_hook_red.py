"""RED：语义门面 lifecycle hook 一等订阅（agent.on(phase, fn)）。

现状裂缝：LifecycleBus 16 相位存在（compose/notify.py），装配期只有事件投影
一个订阅者；门面无订阅入口——agent.runtime.bus 在 say() 之后才存在，且每次
say() 重新装配新 bus，事后订阅错过本次运行、也撑不到下次。"用户可插拔注入，
里面的东西都是自动编排的"缺 hooks 这一格。
"""
from __future__ import annotations

import asyncio

import pytest

from api import Agent
from archs import ReAct
from compose import DangerLevel, LifecyclePhase, ToolSpec
from harness.model import ModelCapability
from harness.tools import tool, clear_pending


@pytest.fixture(autouse=True)
def _isolated_pending_tools() -> None:
    """隔离模块级 @tool 待注册容器：每个测试前清空，测试内自行声明工具。

    @tool 装饰即入列、装配期一次性消费（clear_pending）；若测试依赖模块级
    已装饰工具，前一个测试消费后本测试就注册不到——与 r5 conftest 同款隔离。
    """
    clear_pending()


def _make_weather_tool() -> object:
    """每个测试独立声明一份工具（入列本测试的 _PENDING）。"""

    @tool(name="get_weather", description="查天气", danger=DangerLevel.SAFE)
    def _weather(city: str = "深圳") -> dict[str, object]:
        """本地确定性天气（demo 级替身）。"""
        return {"city": city, "temperature": 28}

    return _weather


class _ToolCallLLM:
    """最小 LLM 替身：首轮发 get_weather 工具调用，次轮收口。"""
    name = "fake"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, ToolCall, Usage
        if not getattr(self, "_called", False):
            self._called = True
            return LLMResponse(
                content="", tool_calls=[ToolCall(call_id="c1", name="get_weather",
                                                 args={"city": "深圳"})],
                usage=Usage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001),
                model="fake",
            )
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=2, completion_tokens=2,
                                       cost_usd=0.0002), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_agent_on_receives_phase_notices() -> None:
    """say() 前注册的 hook 必须在本次运行中收到相位 Notice（当前 FAIL：无 on）。"""
    agent = Agent(ReAct([_make_weather_tool()], llm="fake"))
    agent.use(ModelCapability(llm=_ToolCallLLM()))

    seen: list[LifecyclePhase] = []
    agent.on(LifecyclePhase.AFTER_TOOL, lambda n: seen.append(n.phase))
    agent.on(LifecyclePhase.SESSION_END, lambda n: seen.append(n.phase))

    agent.say("查深圳天气")

    assert LifecyclePhase.AFTER_TOOL in seen, f"after_tool 未送达，实得 {seen}"
    assert LifecyclePhase.SESSION_END in seen, f"session_end 未送达，实得 {seen}"


def test_red_agent_on_notice_carries_context() -> None:
    """hook 收到的 Notice 应带 port/session_id 只读投影上下文。"""
    agent = Agent(ReAct([_make_weather_tool()], llm="fake"))
    agent.use(ModelCapability(llm=_ToolCallLLM()))

    notices: list[object] = []
    agent.on(LifecyclePhase.AFTER_TOOL, notices.append)

    agent.say("查深圳天气")

    assert notices, "after_tool hook 未被调用"
    n = notices[0]
    assert n.port == "tool", f"Notice.port 应为 tool，实得 {n.port!r}"
    assert n.session_id == agent._session_id, (
        f"Notice.session_id 应为 {agent._session_id}，实得 {n.session_id!r}"
    )


def test_red_agent_on_rejects_unknown_phase() -> None:
    """非法相位名必须装配前就报错（fail-closed），不允许静默吞掉。"""
    agent = Agent(ReAct([_make_weather_tool()], llm="fake"))
    try:
        agent.on("not_a_phase", lambda n: None)
    except (TypeError, ValueError):
        return
    raise AssertionError("非法相位未报错")
