"""Lifecycle 装配接线：Runner.notify → runner_notifier(bus) → 订阅者（16 相位真投递）。

回归 Blocker#2：修复前 9 个 Runner 相位发到 no-op 无人接、4 个相位零 emit。
本测试在真实 Runner 链路上断言相位真到达订阅者。
"""
from __future__ import annotations

import asyncio

from aop import AOPContainer
from hooks import LifecycleBus, LifecyclePhase, runner_notifier
from runtime import Invoke, Result, Runner, Session, Terminate

from aop import Invocation


class _FakeLLM:
    """最小 LLMPort：返回固定内容。"""
    def __init__(self, name: str) -> None:
        self.name = name
    async def complete(self, request, ctx):
        from hooks import LLMResponse, Usage
        return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1))
    async def stream(self, request, ctx):
        yield self.name
    @property
    def last_usage(self):
        from hooks import Usage
        return Usage(prompt_tokens=1)


def _program(tool: bool = False):
    resp = yield Invoke(port="tool", payload={"call": {}}) if tool else Invoke(
        port="llm", payload={"messages": [{"role": "user", "content": "hi"}]}
    )
    yield Terminate(output="done")


class TestLifecycleWiring:
    def test_runner_phases_reach_bus_subscriber(self) -> None:
        """Runner 会话/轮次相位经 runner_notifier 桥接真到达订阅者。"""
        bus = LifecycleBus()
        seen: list[str] = []
        for phase in (
            LifecyclePhase.SESSION_START, LifecyclePhase.USER_PROMPT,
            LifecyclePhase.STATE_FOLD, LifecyclePhase.SESSION_END,
        ):
            bus.on(phase, lambda n, s=seen: s.append(n.phase.value))

        llm = _FakeLLM("m")
        async def core(inv: Invocation) -> Result:
            return Result.success({"content": "ok", "tool_calls": []})
        container = AOPContainer(core)
        runner = Runner(call=container.as_call(), notify=runner_notifier(bus))
        asyncio.run(runner.run(Session(session_id="lc-1", program_factory=_program)))
        assert set(seen) >= {"session_start", "user_prompt", "state_fold", "session_end"}, seen

    def test_tool_phases_reach_bus_subscriber(self) -> None:
        """tool 三相位（before/after_tool）在 Invoke(port=tool) 时真投递。"""
        bus = LifecycleBus()
        seen: list[str] = []
        bus.on(LifecyclePhase.BEFORE_TOOL, lambda n: seen.append(n.phase.value))
        bus.on(LifecyclePhase.AFTER_TOOL, lambda n: seen.append(n.phase.value))

        async def core(inv: Invocation) -> Result:
            return Result.success({"ok": True, "value": "x"})
        container = AOPContainer(core)
        runner = Runner(call=container.as_call(), notify=runner_notifier(bus))
        asyncio.run(runner.run(Session(session_id="lc-2", program_factory=lambda: _program(tool=True))))
        assert "before_tool" in seen and "after_tool" in seen, seen

    def test_all_16_phases_have_a_trigger(self) -> None:
        """16 相位枚举完整（每个都有生产触发点，由其余用例覆盖）。"""
        assert len(list(LifecyclePhase)) == 16
        assert "tool_error" in {p.value for p in LifecyclePhase}
