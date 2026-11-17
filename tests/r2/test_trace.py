"""R2-T03：组件留名 + observe 异常隔离。"""
import asyncio

from runtime import AgentState, Effect, EffectKind
from hooks import Decision, HookBus, HookMode
from tests.r2.conftest import BaseHook, make_ctx


def test_leave_names():
    async def run():
        bus = HookBus()
        class A(BaseHook):
            name = "a"; events = ("before_invoke",); mode = HookMode.GUARD
        class B(BaseHook):
            name = "b"; events = ("before_invoke",); mode = HookMode.OBSERVE
        bus.on(A()).on(B()).freeze()
        ctx = make_ctx(Effect(EffectKind.TOOL, {}), AgentState(), "before_invoke")
        await bus.emit("before_invoke", ctx)
        assert any("guard:a" in t for t in ctx.trace)
        assert any("observe:b" in t for t in ctx.trace)
    asyncio.run(run())


def test_observer_exception_isolated():
    async def run():
        bus = HookBus()
        class Bad(BaseHook):
            name = "bad"; events = ("before_invoke",); mode = HookMode.OBSERVE
            async def __call__(self, ctx):
                raise RuntimeError("boom")
        class Guard(BaseHook):
            name = "g"; events = ("before_invoke",); mode = HookMode.GUARD
        bus.on(Bad()).on(Guard()).freeze()
        ctx = make_ctx(Effect(EffectKind.TOOL, {}), AgentState(), "before_invoke")
        d = await bus.emit("before_invoke", ctx)
        assert d.action is Decision.continue_().action
        assert any("error" in t for t in ctx.trace)
    asyncio.run(run())
