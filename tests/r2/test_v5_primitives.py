"""R2 v5 原语测试：5 种 HookKind 语义 + Host/RequestScope/Policy + apply(host)。"""
from __future__ import annotations

import asyncio

import pytest

from hooks import (
    BailHook, Decision, DecisionAction, Host, HookKind, ParallelHook,
    RequestScope, Signal, WaterfallHook,
)


def _decision(action: DecisionAction, target: str = "", value: object = None) -> Decision:
    return Decision(action=action, target=target, value=value)


class TestHookKinds:
    def test_bail_stops_at_first_verdict(self) -> None:
        """Bail：首个非 None 判决即停，后续 tap 不执行。"""
        async def run() -> None:
            calls: list[str] = []
            hook = BailHook("guard")
            hook.tap("a", lambda s, e: calls.append("a") or _decision(DecisionAction.DENY, "blocked"))
            hook.tap("b", lambda s, e: calls.append("b") or _decision(DecisionAction.CONTINUE))
            result = await hook.call(RequestScope())
            assert result is not None and result.action is DecisionAction.DENY
            assert calls == ["a"], "Bail 后不应执行 b"

        asyncio.run(run())

    def test_waterfall_chains_values(self) -> None:
        """Waterfall：上次返回值作下次首参，最终值返回。"""
        async def run() -> None:
            hook = WaterfallHook("transform")
            hook.tap("a", lambda value, e: value + "A")
            hook.tap("b", lambda value, e: value + "B")
            assert await hook.call(RequestScope(), None, "") == "AB"

        asyncio.run(run())

    def test_loop_repeats_until_none(self) -> None:
        """Loop：非 None 返回则重跑整链，上限防死循环。"""
        async def run() -> None:
            calls: list[int] = []

            def attempt(scope: RequestScope, effect, count=[0]) -> object | None:
                count[0] += 1
                calls.append(count[0])
                return "retry" if count[0] < 3 else None

            hook = Host().define("loop", HookKind.LOOP, max_loops=5)
            hook.tap("attempt", attempt)
            await hook.call(RequestScope())
            assert calls == [1, 2, 3]

        asyncio.run(run())

    def test_loop_hard_limit(self) -> None:
        """Loop 超过 max_loops 抛错，不无限重跑。"""
        async def run() -> None:
            hook = Host().define("loop", HookKind.LOOP, max_loops=2)
            hook.tap("always", lambda scope, e: "again")
            with pytest.raises(RuntimeError, match="max_loops"):
                await hook.call(RequestScope())

        asyncio.run(run())

    def test_parallel_runs_all(self) -> None:
        """Parallel：全部 tap 执行（异常隔离），返回 None。"""
        async def run() -> None:
            calls: list[str] = []
            hook = ParallelHook("observe")
            hook.tap("a", lambda s, e: calls.append("a"))
            hook.tap("b", lambda s, e: calls.append("b"))
            assert await hook.call(RequestScope()) is None
            assert set(calls) == {"a", "b"}

        asyncio.run(run())

    def test_around_wraps_next(self) -> None:
        """Around：tap 收 next 回调，前后介入，中心只调一次。"""
        async def run() -> None:
            order: list[str] = []
            hook = Host().define("around", HookKind.AROUND)
            hook.tap("outer", lambda next_fn, s, e: _around(order, "outer", next_fn, s))
            hook.tap("inner", lambda next_fn, s, e: _around(order, "inner", next_fn, s))

            async def core(scope: RequestScope) -> Decision:
                order.append("core")
                return _decision(DecisionAction.CONTINUE, value="ok")

            result = await hook.call(RequestScope(), core=core)
            assert order == ["outer-in", "inner-in", "core", "inner-out", "outer-out"]
            assert result.value == "ok"

        asyncio.run(run())


async def _around(order: list[str], name: str, next_fn, scope: RequestScope) -> Decision:
    order.append(f"{name}-in")
    result = await next_fn()
    order.append(f"{name}-out")
    return result


class TestHost:
    def test_request_scope_isolated(self) -> None:
        """每次 new_scope 独立，signals 不串。"""
        host = Host()
        s1 = host.new_scope()
        s2 = host.new_scope()
        s1.emit(Signal("degrade", "boom"))
        assert len(s1.signals) == 1 and s2.signals == []

    def test_plugin_apply_registers(self) -> None:
        """Plugin 只实现 apply(host)，无 mode/priority/events。"""
        class MyPlugin:
            name = "my"

            def apply(self, host: Host) -> None:
                host.hook("before_invoke").tap(self.name, lambda s, e: _decision(DecisionAction.CONTINUE))

        host = Host()
        host.define("before_invoke", HookKind.BAIL)
        assert host.plugin(MyPlugin()) is host
        assert host.hook("before_invoke").names == ("my",)
