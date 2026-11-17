"""R2-T01：四 mode × 事件组合。"""
import asyncio

from runtime import AgentState, Effect, EffectKind
from hooks import Decision, DecisionAction, HookBus, HookMode
from tests.r2.conftest import BaseHook, make_ctx


def test_four_modes_dispatch():
    async def run():
        bus = HookBus()
        order = []

        class Obs(BaseHook):
            name = "obs"; events = ("before_invoke",); mode = HookMode.OBSERVE; priority = 1
            async def __call__(self, ctx):
                order.append("obs"); return Decision.continue_()

        class Guard(BaseHook):
            name = "guard"; events = ("before_invoke",); mode = HookMode.GUARD; priority = 2
            async def __call__(self, ctx):
                order.append("guard"); return Decision.continue_()

        class Trans(BaseHook):
            name = "trans"; events = ("before_invoke",); mode = HookMode.TRANSFORM; priority = 3
            async def __call__(self, ctx):
                order.append("trans"); return Decision.rewrite(context="c")

        class Around(BaseHook):
            name = "around"; events = ("before_invoke",); mode = HookMode.AROUND; priority = 4
            async def __call__(self, ctx):
                order.append("around-in")
                await ctx.meta["next"]()
                order.append("around-out")
                return Decision.continue_()

        bus.on(Obs()).on(Guard()).on(Trans()).on(Around()).freeze()
        ctx = make_ctx(Effect(EffectKind.TOOL, {}), AgentState(), "before_invoke")
        ctx.invoke = lambda e, c: asyncio.sleep(0, result=None)
        d = await bus.emit("before_invoke", ctx)
        assert d.action is DecisionAction.REWRITE  # transform 的 rewrite 被合并
        assert order.index("obs") < order.index("guard") < order.index("trans") < order.index("around-in")
        assert order[-1] == "around-out"
        assert ctx.meta["injected_context"] == ["c"]
    asyncio.run(run())
