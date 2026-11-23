"""RED：装配 bus 共享给模型网关（before_model/after_model 门面可达）。

裂缝：ModelCapability.dispatch() 自建 gateway（bus=构造参数，默认 None），
门面从未把装配 bus 共享给它 → 模型相位 0 发射：agent.on 收不到、事件流投影
也看不到模型事件。探针实测 16 相位里 before_model/after_model 恒 0。
"""
from __future__ import annotations

import pytest

from api import Agent
from archs import ReAct
from compose import DangerLevel, LifecyclePhase, ToolSpec
from harness.model import ModelCapability
from harness.tools import tool, clear_pending


@pytest.fixture(autouse=True)
def _isolated_pending_tools() -> None:
    """隔离 @tool 模块级待注册容器（与 r5 conftest 同款）。"""
    clear_pending()


class _PlainLLM:
    name = "fake"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _make_agent() -> Agent:
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=_PlainLLM()))
    return agent


def test_red_before_after_model_reach_facade_hooks() -> None:
    """say() 运行中模型相位必须进装配 bus（hook 与事件流同一总线）。"""
    agent = _make_agent()
    seen: list[LifecyclePhase] = []
    agent.on(LifecyclePhase.BEFORE_MODEL, lambda n: seen.append(n.phase))
    agent.on(LifecyclePhase.AFTER_MODEL, lambda n: seen.append(n.phase))

    agent.say("你好")

    assert LifecyclePhase.BEFORE_MODEL in seen, (
        f"before_model 未进装配 bus，实得 {[p.value for p in seen]}")
    assert LifecyclePhase.AFTER_MODEL in seen, (
        f"after_model 未进装配 bus，实得 {[p.value for p in seen]}")


def test_red_model_phase_notice_carries_port_llm() -> None:
    """模型相位 Notice.port 应为 'llm'（与 tool 相位区分）。"""
    agent = _make_agent()
    ports: list[str] = []
    agent.on(LifecyclePhase.AFTER_MODEL, lambda n: ports.append(n.port))

    agent.say("你好")

    assert ports and all(p == "llm" for p in ports), f"port 应为 llm，实得 {ports}"
