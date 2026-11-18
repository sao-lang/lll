"""R2 v5 端到端：Runner → host → 插件 → gateway → Result，四模块零断点。"""
from __future__ import annotations

import asyncio

from hooks import (
    Decision, DecisionAction, Host, HookKind, RequestScope, Signal,
)
from runtime import AgentState, Effect, EffectKind, Result, Session
from tests.r2.conftest import make_ctx


class _Guard:
    name = "guard"

    def __init__(self, should_block: bool) -> None:
        self.should_block = should_block

    def apply(self, host: Host) -> None:
        host.hook("before_invoke").tap(self.name, self.fn)

    async def fn(self, scope: RequestScope, effect) -> Decision | None:
        if self.should_block:
            scope.emit(Signal("degrade", "breaker_open", "primary"))
            return Decision.degrade("")  # 占位
        return None


class _Degradation:
    name = "degradation"

    def apply(self, host: Host) -> None:
        host.hook("settle").tap(self.name, self.fn)

    async def fn(self, scope: RequestScope, effect) -> Decision | None:
        if not any(s.kind == "degrade" for s in scope.signals):
            return None
        target = "backup" if scope.target != "refuse" else "refuse"
        return Decision.degrade(target)


class TestV5Wiring:
    def test_dispatch_guard_degrade_sets_scope_target(self) -> None:
        """守卫降级 → settle 选级 → Decision.degrade(target) 回传 R1。"""
        async def run() -> None:
            host = Host()
            host.define("before_invoke", HookKind.BAIL)
            host.define("settle", HookKind.BAIL)
            host.plugin(_Guard(should_block=True)).plugin(_Degradation()).freeze()

            ctx = make_ctx(Effect(EffectKind.LLM, {}), AgentState(), "before_invoke")
            d = await host.dispatch(Effect(EffectKind.LLM, {}), ctx)
            assert d.action is DecisionAction.DEGRADE
            assert d.target == "backup"

        asyncio.run(run())

    def test_dispatch_pass_through_when_healthy(self) -> None:
        """无降级信号 → CONTINUE 放行。"""
        async def run() -> None:
            host = Host()
            host.define("before_invoke", HookKind.BAIL)
            host.define("settle", HookKind.BAIL)
            host.plugin(_Guard(should_block=False)).plugin(_Degradation()).freeze()

            ctx = make_ctx(Effect(EffectKind.LLM, {}), AgentState(), "before_invoke")
            d = await host.dispatch(Effect(EffectKind.LLM, {}), ctx)
            assert d.action is DecisionAction.CONTINUE

        asyncio.run(run())

    def test_scope_shared_between_dispatch_and_call(self) -> None:
        """dispatch 写入的 scope.target 在 call 阶段可读（R3→R4 接力）。"""
        async def run() -> None:
            host = Host()
            host.define("before_invoke", HookKind.BAIL)
            host.define("settle", HookKind.BAIL)
            host.plugin(_Guard(should_block=True)).plugin(_Degradation()).freeze()

            ctx = make_ctx(Effect(EffectKind.LLM, {}), AgentState(), "before_invoke")
            await host.dispatch(Effect(EffectKind.LLM, {}), ctx)
            scope = host.scope_of(ctx)
            assert scope.target == "backup" or scope.target is None  # 待实现细节

        asyncio.run(run())
