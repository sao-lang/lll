"""R2 v5 host.call：AROUND 包裹 + after_invoke/invoke_error 观察（修复 last_result 断点）。"""
from __future__ import annotations

import asyncio

from hooks import Decision, Host, HookKind, RequestScope, Signal
from runtime import Result
from tests.r2.conftest import make_ctx


class _Observer:
    name = "observer"

    def __init__(self) -> None:
        self.after_seen: list[object] = []
        self.error_seen: list[object] = []

    def apply(self, host: Host) -> None:
        host.hook("after_invoke").tap(self.name, self.after)
        host.hook("invoke_error").tap(self.name, self.error)

    async def after(self, scope: RequestScope, effect) -> None:
        self.after_seen.append(scope.last_result)

    async def error(self, scope: RequestScope, effect) -> None:
        self.error_seen.append(scope.last_result)


class TestHostCall:
    def test_call_wraps_core_and_records_result(self) -> None:
        """host.call 包裹 inner，成功写 last_result 并触发 after_invoke。"""
        async def run() -> None:
            host = Host()
            obs = _Observer()
            host.plugin(obs).freeze()

            async def inner(effect, ctx):
                return Result.success("ok")

            wrapped = host.call(inner)
            ctx = make_ctx(effect=None, state=None)  # type: ignore[arg-type]
            await wrapped(None, ctx)
            assert len(obs.after_seen) == 1
            assert obs.after_seen[0].ok is True

        asyncio.run(run())

    def test_call_records_error_result(self) -> None:
        """inner 抛异常 → last_result 写入 error 并触发 invoke_error。"""
        async def run() -> None:
            host = Host()
            obs = _Observer()
            host.plugin(obs).freeze()

            async def inner(effect, ctx):
                raise RuntimeError("boom")

            wrapped = host.call(inner)
            ctx = make_ctx(effect=None, state=None)  # type: ignore[arg-type]
            try:
                await wrapped(None, ctx)
            except RuntimeError:
                pass
            assert len(obs.error_seen) == 1
            assert obs.error_seen[0].ok is False

        asyncio.run(run())

    def test_around_can_call_core_multiple_times(self) -> None:
        """around_invoke 的 tap 可多次调 next_fn（retry 的能力基础）。"""
        async def run() -> None:
            host = Host()
            calls: list[int] = []

            class DualCall:
                name = "dual"

                def apply(self, h: Host) -> None:
                    h.hook("around_invoke").tap(self.name, self.fn)

                async def fn(self, next_fn, scope: RequestScope, effect) -> Decision:
                    first = await next_fn()
                    second = await next_fn()
                    return second

            host.plugin(DualCall()).freeze()

            async def inner(effect, ctx):
                calls.append(1)
                return Result.success("ok")

            wrapped = host.call(inner)
            ctx = make_ctx(effect=None, state=None)  # type: ignore[arg-type]
            await wrapped(None, ctx)
            assert len(calls) == 2

        asyncio.run(run())
