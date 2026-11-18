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

    def test_short_circuit_does_not_leak_scope_bridge(self) -> None:
        """C2：守卫短路（本轮无 call）须清理 scope_bridge，避免陈旧 scope + id 复用串味。"""
        async def run() -> None:
            host = Host()
            host.define("before_invoke", HookKind.BAIL)
            host.define("settle", HookKind.BAIL)
            host.plugin(_Guard(should_block=True)).plugin(_Degradation()).freeze()

            # 多个不同的 ctx 依次短路，每个都不应残留在 bridge 里
            for i in range(10):
                ctx = make_ctx(Effect(EffectKind.LLM, {}), AgentState(), "before_invoke")
                await host.dispatch(Effect(EffectKind.LLM, {}), ctx)
                assert host.scope_of(ctx) is None, f"短路后 ctx#{i} 的 scope 未清理"
            assert len(host._scope_bridge) == 0, \
                f"10 次短路后 bridge 应为空，实际残留 {len(host._scope_bridge)} 个"

        asyncio.run(run())

    def test_scope_shared_between_dispatch_and_call(self) -> None:
        """健康路径：dispatch 创建的 scope 在 call 阶段可经 bridge 取回（R3→R4 接力）。"""
        async def run() -> None:
            host = Host()
            host.define("before_invoke", HookKind.BAIL)
            host.define("settle", HookKind.BAIL)
            # 健康守卫（不短路），dispatch 后 scope 应留在 bridge 供 call 接力
            host.plugin(_Guard(should_block=False)).plugin(_Degradation()).freeze()

            ctx = make_ctx(Effect(EffectKind.LLM, {}), AgentState(), "before_invoke")
            await host.dispatch(Effect(EffectKind.LLM, {}), ctx)
            scope = host.scope_of(ctx)
            assert scope is not None, "健康路径 dispatch 后 scope 应留在 bridge 供 call 读取"

        asyncio.run(run())
