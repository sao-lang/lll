"""R2-T02：8 种 Decision.action 各自生效。"""
import asyncio

from runtime import AgentState, Effect, EffectKind
from hooks import Decision, DecisionAction, HookBus, HookMode
from tests.r2.conftest import BaseHook, make_ctx


async def emit_with(decision: Decision):
    bus = HookBus()
    class G(BaseHook):
        name = "g"; events = ("before_invoke",); mode = HookMode.GUARD
        async def __call__(self, ctx): return decision
    bus.on(G()).freeze()
    ctx = make_ctx(Effect(EffectKind.TOOL, {}), AgentState(), "before_invoke")
    return await bus.emit("before_invoke", ctx), ctx


def test_all_actions():
    async def run():
        cases = [
            Decision.continue_(), Decision.rewrite(context="x"), Decision.deny("no"),
            Decision.ask("q", ["a", "b"]), Decision.retry_(), Decision.degrade("backup"),
            Decision.fallback("v"), Decision.abort("stop"),
        ]
        expected = [
            DecisionAction.CONTINUE, DecisionAction.REWRITE, DecisionAction.DENY,
            DecisionAction.ASK, DecisionAction.RETRY, DecisionAction.DEGRADE,
            DecisionAction.FALLBACK, DecisionAction.ABORT,
        ]
        for decision, want in zip(cases, expected):
            d, _ = await emit_with(decision)
            assert d.action is want, f"{decision} -> {d.action}"
    asyncio.run(run())
