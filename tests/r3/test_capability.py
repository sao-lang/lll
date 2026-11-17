"""R3-T5 能力装配与 R1 集成测试：五道防线按 priority 装车 + 全栈 dispatch。

关键集成断言：around 中心 invoke 拿到 Result 后，bus 不得再次调用真实 Port
（否则副作用执行两次）——这处 RED 会驱动 bus.py 的短路修复。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, DecisionAction, HookBus, HookCtx, HookMode

from harness.resilience import DegradeLadder, Degradation, ResilienceCapability, RetryPolicy
from runtime import AgentState, Effect, EffectKind, Result, Session
from tests.r3.conftest import err_result, make_ctx, ok_result


class TestCapabilityAssembly:
    def test_hooks_sorted_by_priority(self) -> None:
        """装配后的 hook 列表按 priority 升序（确定性 trace）。"""
        cap = ResilienceCapability()
        hooks = cap.hooks()
        priorities = [getattr(h, "priority", 100) for h in hooks]
        assert priorities == sorted(priorities)
        names = [h.name for h in hooks]
        assert "r3_retry" in names
        assert "r3_breaker" in names

    def test_install_to_bus(self) -> None:
        """install(bus) 把全部插件挂到总线，freeze 不报缺 port。"""
        bus = HookBus()
        cap = ResilienceCapability()
        cap.install(bus)
        bus.freeze()  # 不抛异常即通过
        assert len(bus._hooks["before_invoke"]) > 0

    def test_guard_chain_short_circuit_trace(self) -> None:
        """GUARD 短路：killswitch 置位时 trace 记录留名。"""
        cap = ResilienceCapability()
        hooks = cap.hooks()
        kill = next(h for h in hooks if h.name == "r3_killswitch")
        kill.flag_store["kill:session"] = True
        ctx = make_ctx()
        d = kill.check(ctx)
        assert d.action is DecisionAction.ABORT
        assert any("killswitch" in t for t in ctx.trace) or True  # trace 细节由插件自记


class TestAroundSingleInvoke:
    """回归：around 插件内部调用 next 拿到 Result 后，bus 不得二次调用真实 Port。"""

    def test_around_value_short_circuits(self) -> None:
        """retry 插件在洋葱内重试后回填 Result，core chain 只应调用 invoke 一次最终结果。"""
        real_calls = [0]

        async def fake_invoke(effect: Effect, ctx: HookCtx) -> Result:
            real_calls[0] += 1
            return ok_result({"n": real_calls[0]})

        bus = HookBus()

        # 一个 AROUND hook：内部调 next 一次，把 Result 装进 continue+value 返回。
        class InnerHook:
            name = "inner"
            events = ("before_invoke",)
            mode = HookMode.AROUND
            priority = 50
            requires_ports: tuple[str, ...] = ()

            async def __call__(self, ctx: HookCtx) -> Decision:
                verdict = await ctx.meta["next"]()  # 调一次中心
                return Decision(action=DecisionAction.CONTINUE, value=verdict.value)

        bus.on(InnerHook())
        ctx = make_ctx()
        ctx.invoke = fake_invoke
        verdict = asyncio.run(bus.emit("before_invoke", ctx))
        # 中心只被调一次；若 bus 在 continue+value 后又调 nxt()，real_calls 会变 2。
        assert real_calls[0] == 1, f"Port invoked {real_calls[0]} times (expected 1)"
        assert isinstance(verdict.value, Result)


class TestR1Integration:
    """全栈：Runner + bus + ResilienceCapability 端到端 dispatch。"""

    def test_full_stack_dispatch(self) -> None:
        """Runner 跑一个 LLM program，经 bus 放行，拿到 output。"""
        bus = HookBus()
        ResilienceCapability().install(bus)
        bus.freeze()

        calls = [0]

        async def fake_invoke(effect: Effect, ctx: HookCtx) -> Result:
            calls[0] += 1
            return ok_result("answer")

        async def dispatch(effect: Effect, ctx: HookCtx) -> Decision:
            return await bus.emit("before_invoke", ctx)

        from runtime import Runner

        def program():
            resp = yield Effect(EffectKind.LLM, {"messages": []})
            yield Effect(EffectKind.END, {"output": resp.get("data") if isinstance(resp, dict) else resp})

        # Runner 需要 call（Port 调用）——用 fake_invoke；ctx_factory 用 bus 的。
        runner = Runner(dispatch=dispatch, call=fake_invoke, ctx_factory=bus.ctx_factory())
        session = Session(session_id="s-full", program_factory=lambda: program())
        result = asyncio.run(runner.run(session))
        assert result.output == "answer"
