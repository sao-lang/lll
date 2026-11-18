"""端到端接线测试（ARCH-v5）：Runner → host.dispatch → 插件 → host.call(gw.as_call) → Result。

保留领域逻辑：降级切端点 / after_invoke 观察 tap / 预算软提示 / 精确缓存。
宿主是 Host（替代旧 HookBus），降级后端用 ModelCapability.add_backend 注册。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, Host, LLMRequest, LLMResponse, PortCtx, RequestScope, Signal, Usage
from runtime import AgentState, Budgets, Effect, EffectKind, Result, Runner, Session

from harness.model import ModelCapability
from harness.resilience import (
    BudgetGuard, DegradeLadder, DegradeMode, DegradeStep, Degradation,
)


class FakeLLM:
    name = ""
    last_usage = Usage()

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.name, model=self.name, usage=Usage())

    def stream(self, request: LLMRequest, ctx: PortCtx):
        async def gen():
            yield self.name
        return gen()


class _ForceBreaker:
    """模拟熔断打开：before_invoke 报降级信号；settle 由 Degradation 选 backup。"""

    name = "force_breaker"

    def __init__(self, ladder: DegradeLadder, available: set[str]) -> None:
        self.degradation = Degradation(ladder, available=available)

    def apply(self, host: Host) -> None:
        class Guard:
            name = "guard"

            def apply(self, h: Host) -> None:
                h.hook("before_invoke").tap(self.name, self.check)

            async def check(self, scope: RequestScope, effect) -> Decision:
                scope.emit(Signal("degrade", "breaker_open", "primary"))
                return Decision.degrade("")  # 占位，settle 覆盖

        host.plugin(Guard()).plugin(self.degradation)


class _AfterInvokeObserver:
    """观察 tap：记录 host.call 写入 scope.last_result 的结果。"""

    name = "after_invoke_observer"

    def __init__(self) -> None:
        self.seen: list[object] = []

    def apply(self, host: Host) -> None:
        host.hook("after_invoke").tap(self.name, self.fn)

    async def fn(self, scope: RequestScope, effect) -> None:
        self.seen.append(scope.last_result)


class _TransformSpy:
    """观察 transform（WATERFALL）链式改写后的文本值。"""

    name = "transform_spy"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def apply(self, host: Host) -> None:
        host.hook("transform").tap(self.name, self.fn)

    def fn(self, value: str, effect) -> str:
        self.seen.append(value)
        return value


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():
    resp = yield Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]})
    content = resp.get("content", "?") if isinstance(resp, dict) else "?"
    yield Effect(EffectKind.END, {"output": content})


class TestEndToEndWiring:
    def test_degrade_switches_endpoint_in_full_chain(self) -> None:
        """全链：Runner → host.dispatch(降级) → as_call → backup 实例，primary 零调用。"""
        primary = FakeLLM("primary")
        backup = FakeLLM("backup")
        cap = ModelCapability(llm=primary)
        cap.add_backend("backup", backup)
        gw = cap.gateway()

        host = Host()
        host.plugin(_ForceBreaker(_ladder(), {"primary", "backup"})).freeze()

        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="e2e-degrade", program_factory=_program)
        result = asyncio.run(runner.run(session))
        assert result.output == "backup", f"降级未切端点：{result.output}"
        assert backup.calls == 1 and primary.calls == 0

    def test_after_invoke_observer_tapped_in_full_chain(self) -> None:
        """全链：host.call 真实触发 after_invoke，观察 tap 拿到 last_result。"""
        primary = FakeLLM("primary")
        cap = ModelCapability(llm=primary)
        gw = cap.gateway()

        observer = _AfterInvokeObserver()
        host = Host()
        host.plugin(observer).freeze()

        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="e2e-events", program_factory=_program)
        asyncio.run(runner.run(session))
        assert len(observer.seen) == 1, "after_invoke tap 未触发"
        last = observer.seen[0]
        assert isinstance(last, Result) and last.ok, "after_invoke 未观察到成功的 last_result"

    def test_budget_guard_triggers_in_full_chain(self) -> None:
        """BudgetGuard 的 transform（WATERFALL）在全链真改写上下文文本。"""
        primary = FakeLLM("primary")
        cap = ModelCapability(llm=primary)
        gw = cap.gateway()

        tight = Budgets(max_turns=2, used_turns=1)  # 50% ≥ soft=0.5
        guard = BudgetGuard(soft=0.5, budgets=tight)
        spy = _TransformSpy()
        host = Host()
        host.plugin(guard).plugin(spy).freeze()

        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        state = AgentState(budgets=tight)
        session = Session(session_id="e2e-budget", program_factory=_program, state=state)
        asyncio.run(runner.run(session))
        assert any("预算即将用尽" in text for text in spy.seen), \
            "BudgetGuard 的 transform 在全链里从未注入收尾提示"

    def test_exact_cache_hit_in_full_chain(self) -> None:
        """同一请求经两个 Runner 复用同一网关：第二次缓存命中，后端只调一次。"""
        primary = FakeLLM("primary")
        cap = ModelCapability(llm=primary)
        gw = cap.gateway()
        host = Host().freeze()

        runner1 = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        res1 = asyncio.run(runner1.run(Session(session_id="e2e-cache-1", program_factory=_program)))
        runner2 = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        res2 = asyncio.run(runner2.run(Session(session_id="e2e-cache-2", program_factory=_program)))

        assert res1.output == "primary" and res2.output == "primary"
        assert primary.calls == 1, f"精确缓存未命中：后端被调用 {primary.calls} 次"
