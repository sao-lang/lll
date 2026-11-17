"""R2 总线适配器测试（v2.1）：bus.dispatch 补事件 emit + 透传 DEGRADE target。

接线断点修复：插件订阅的 before_model/after_invoke/invoke_error 事件生产代码零 emit，
DEGRADE decision 无消费者。bus.dispatch()/call() 适配器补齐 emit + 接力。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, DecisionAction, HookBus, HookCtx, HookMode
from runtime import AgentState, Effect, EffectKind


def _ctx(effect: Effect | None = None, event: str = "before_invoke") -> HookCtx:
    return HookCtx(
        effect=effect or Effect(EffectKind.LLM, {"messages": []}),
        state=AgentState(), session_id="s", event=event,
    )


class _RecordingHook:
    name = "rec"
    priority = 0
    requires_ports: tuple[str, ...] = ()

    def __init__(self, mode: HookMode, events: tuple[str, ...], action: DecisionAction, target: str = "") -> None:
        self.mode, self.events, self.action, self.target = mode, events, action, target
        self.seen: list[str] = []

    async def __call__(self, ctx: HookCtx) -> Decision:
        self.seen.append(ctx.event)
        if self.action is DecisionAction.DEGRADE:
            return Decision.degrade(self.target)
        if self.action is DecisionAction.REWRITE:
            return Decision.rewrite(context="hint")
        return Decision.continue_()


class TestBusDispatchEmitsFullEvents:
    def test_llm_effect_emits_before_model_then_invoke(self) -> None:
        """LLM Effect：dispatch 适配器依次 emit before_model + before_invoke。"""
        bus = HookBus()
        rec = _RecordingHook(HookMode.OBSERVE, ("before_model", "before_invoke"), DecisionAction.CONTINUE)
        bus.on(rec)  # type: ignore[arg-type]
        ctx = _ctx()
        asyncio.run(bus.dispatch()(Effect(EffectKind.LLM, {"messages": []}), ctx))
        assert "before_model" in rec.seen and "before_invoke" in rec.seen
        assert rec.seen.index("before_model") < rec.seen.index("before_invoke")

    def test_tool_effect_only_emits_before_invoke(self) -> None:
        bus = HookBus()
        rec = _RecordingHook(HookMode.OBSERVE, ("before_invoke",), DecisionAction.CONTINUE)
        bus.on(rec)  # type: ignore[arg-type]
        ctx = _ctx()
        asyncio.run(bus.dispatch()(Effect(EffectKind.TOOL, {"call": "x"}), ctx))
        assert rec.seen == ["before_invoke"]

    def test_degrade_decision_passthrough_and_target_in_ctx(self) -> None:
        """DEGRADE 决策透传 + target 写进 ctx.meta 供 call 适配器接力。"""
        bus = HookBus()
        bus.on(_RecordingHook(HookMode.GUARD, ("before_invoke",), DecisionAction.DEGRADE, "backup"))  # type: ignore[arg-type]
        ctx = _ctx()
        d = asyncio.run(bus.dispatch()(Effect(EffectKind.TOOL, {"call": "x"}), ctx))
        assert d.action is DecisionAction.DEGRADE
        assert ctx.meta.get("degrade_target") == "backup"


class TestBusCallEmitsPostEvents:
    def test_call_emits_after_invoke(self) -> None:
        """call 适配器在成功后 emit after_invoke（喂熔断计数）。"""
        bus = HookBus()
        rec = _RecordingHook(HookMode.OBSERVE, ("after_invoke",), DecisionAction.CONTINUE)
        bus.on(rec)  # type: ignore[arg-type]

        async def inner(effect: Effect, ctx: HookCtx) -> object:
            return "ok"

        wrapped = bus.call(inner)
        asyncio.run(wrapped(Effect(EffectKind.TOOL, {"call": "x"}), _ctx()))
        assert "after_invoke" in rec.seen
