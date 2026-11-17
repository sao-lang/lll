"""端到端接线测试（v1.5）：R1 Runner + R2 bus.dispatch + R4 gateway.as_call 全链。

证明三处断点真修好：DEGRADE 接力切端点 / before_model 真触发 / after_invoke 真 emit。
这是"R1~R4 串起来"的可运行样板（此前只有测试里手搓 dispatch）。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, DecisionAction, HookBus, HookCtx, HookMode, LLMRequest, LLMResponse, PortCtx, Usage
from runtime import AgentState, Effect, EffectKind, Session

from harness.model import InMemoryModelRegistry, ModelGateway
from harness.resilience import BudgetGuard


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


class _ForceDegrade:
    """GUARD hook：强制降级到 backup（模拟熔断打开/预算降级）。"""
    name = "force_degrade"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 0
    requires_ports: tuple[str, ...] = ()

    async def __call__(self, ctx: HookCtx) -> Decision:
        return Decision.degrade("backup")


class _EventRecorder:
    name = "recorder"
    events = ("before_model", "after_invoke", "invoke_error")
    mode = HookMode.OBSERVE
    priority = 0
    requires_ports: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, ctx: HookCtx) -> Decision:
        self.seen.append(ctx.event)
        return Decision.continue_()


def _program():
    # R1 语义：yield 返回值是 Result.data（此处 as_call 回填的 dict），不是 Result 本身
    resp = yield Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]})
    content = resp.get("content", "?") if isinstance(resp, dict) else "?"
    yield Effect(EffectKind.END, {"output": content})


class TestEndToEndWiring:
    def test_degrade_switches_endpoint_in_full_chain(self) -> None:
        """全链：Runner → bus.dispatch(DEGRADE) → gateway.as_call → backup 实例。"""
        primary = FakeLLM("primary")
        backup = FakeLLM("backup")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        reg.register_llm("backup", backup)
        gw = ModelGateway(llm=primary, registry=reg)

        bus = HookBus()
        bus.on(_ForceDegrade())  # type: ignore[arg-type]
        from runtime import Runner
        runner = Runner(
            dispatch=bus.dispatch(),  # type: ignore[arg-type]
            call=gw.as_call(),        # type: ignore[arg-type]
            ctx_factory=bus.ctx_factory(),
        )
        session = Session(session_id="e2e", program_factory=lambda: _program())
        result = asyncio.run(runner.run(session))
        assert result.output == "backup", f"降级未切端点，output={result.output}"
        assert backup.calls == 1 and primary.calls == 0

    def test_events_emitted_in_full_chain(self) -> None:
        """全链：before_model / after_invoke 被真实 emit（不再零触发）。"""
        primary = FakeLLM("primary")
        gw = ModelGateway(llm=primary)
        bus = HookBus()
        rec = _EventRecorder()
        bus.on(rec)  # type: ignore[arg-type]
        from runtime import Runner
        runner = Runner(
            dispatch=bus.dispatch(),          # type: ignore[arg-type]
            call=bus.call(gw.as_call()),      # type: ignore[arg-type]  wrapper 补 after_invoke emit
            ctx_factory=bus.ctx_factory(),
        )
        session = Session(session_id="e2e-ev", program_factory=lambda: _program())
        asyncio.run(runner.run(session))
        assert "before_model" in rec.seen, "before_model 未 emit"
        assert "after_invoke" in rec.seen, "after_invoke 未 emit"

    def test_budget_guard_triggers_in_full_chain(self) -> None:
        """BudgetGuard（TRANSFORM/before_model）在全链里真触发（此前事件零 emit 空转）。"""
        primary = FakeLLM("primary")
        gw = ModelGateway(llm=primary)
        bus = HookBus()
        from runtime import Budgets, Runner
        tight = Budgets(max_turns=2, used_turns=1)  # 50% ≥ soft=0.5
        guard = BudgetGuard(soft=0.5)

        # spy：观察 BudgetGuard 的 rewrite 是否真注入了收尾上下文
        rewrites: list[str] = []
        orig_check = guard.check
        def spy_check(ctx):
            d = orig_check(ctx)
            if d.context_inject:
                rewrites.append(d.context_inject)
            return d
        guard.check = spy_check  # type: ignore[method-assign]

        bus.on(guard)  # type: ignore[arg-type]
        runner = Runner(
            dispatch=bus.dispatch(),  # type: ignore[arg-type]
            call=gw.as_call(),        # type: ignore[arg-type]
            ctx_factory=bus.ctx_factory(),
        )
        state = AgentState(budgets=tight)
        session = Session(session_id="e2e-bg", program_factory=lambda: _program(), state=state)
        asyncio.run(runner.run(session))
        assert rewrites, "BudgetGuard 的 before_model 在全链里从未触发（事件仍空转）"
