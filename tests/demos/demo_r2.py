"""R2 独立 demo：5 种 HookKind + Host.define/plugin/freeze + RequestScope。

插件只实现 apply(host) 把回调挂到宿主定义的扩展点上；宿主选定 Bail /
Waterfall / Loop / Parallel / Around 语义；每次调用各用一个独立 RequestScope。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import (  # noqa: E402
    Decision, DecisionAction, HookCtx, HookKind, Host, RequestScope, Signal,
)
from runtime import AgentState, Effect, EffectKind  # noqa: E402


class DemoPlugin:
    """一个插件在 apply 中把回调挂到五类扩展点。"""

    name = "r2_demo"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.attempt_n = 0

    def apply(self, host: Host) -> None:
        host.hook("guard").tap(self.name, self.guard)
        host.hook("guard").tap(self.name + "_late", self.never)
        host.hook("rewrite").tap(self.name, self.append_a)
        host.hook("rewrite").tap(self.name + "_2", self.append_b)
        host.hook("looper").tap(self.name, self.attempt)
        host.hook("observe").tap(self.name, self.observe_a)
        host.hook("observe").tap(self.name + "_2", self.observe_b)
        host.hook("wrap").tap(self.name, self.outer)
        host.hook("wrap").tap(self.name + "_2", self.inner)

    async def guard(self, scope: RequestScope, effect: Effect) -> Decision | None:
        self.calls.append("guard")
        if effect.payload.get("block"):
            return Decision.deny("blocked")
        return None

    async def never(self, scope: RequestScope, effect: Effect) -> Decision | None:
        self.calls.append("never")
        return None

    def append_a(self, value: str, effect: Effect) -> str:
        self.calls.append("a")
        return value + "A"

    def append_b(self, value: str, effect: Effect) -> str:
        self.calls.append("b")
        return value + "B"

    def attempt(self, scope: RequestScope, effect: Effect) -> str | None:
        self.attempt_n += 1
        self.calls.append(f"attempt{self.attempt_n}")
        return "again" if self.attempt_n < 3 else None

    async def observe_a(self, scope: RequestScope, effect: Effect) -> None:
        self.calls.append("observe_a")
        if effect.payload.get("raise"):
            raise RuntimeError("observe failure isolated")

    async def observe_b(self, scope: RequestScope, effect: Effect) -> None:
        self.calls.append("observe_b")

    async def outer(self, nxt: object, scope: RequestScope, effect: Effect) -> object:
        self.calls.append("outer-in")
        result = await nxt()  # type: ignore[operator]
        self.calls.append("outer-out")
        return result

    async def inner(self, nxt: object, scope: RequestScope, effect: Effect) -> object:
        self.calls.append("inner-in")
        result = await nxt()  # type: ignore[operator]
        self.calls.append("inner-out")
        return result


class DispatchGuard:
    """挂载标准 before_invoke（BAIL），演示 R1 注入点。"""

    name = "r2_dispatch_guard"

    def apply(self, host: Host) -> None:
        host.hook("before_invoke").tap(self.name, self.check)

    async def check(self, scope: RequestScope, effect: Effect) -> Decision:
        return Decision.deny("dispatch_blocked")


async def main() -> None:
    host = Host()
    host.define("guard", HookKind.BAIL)
    host.define("rewrite", HookKind.WATERFALL)
    host.define("looper", HookKind.LOOP, max_loops=5)
    host.define("observe", HookKind.PARALLEL)
    host.define("wrap", HookKind.AROUND)
    demo = DemoPlugin()
    host.plugin(demo).plugin(DispatchGuard()).freeze()
    eff = Effect(EffectKind.LLM, {})

    # 1) Bail：首个非 CONTINUE 判决短路，后续 tap 不执行
    guard_scope = host.new_scope()
    blocked = Effect(EffectKind.LLM, {"block": True})
    verdict = await host.hook("guard").call(guard_scope, blocked)
    assert verdict is not None and verdict.action is DecisionAction.DENY
    assert "never" not in demo.calls

    # 2) Waterfall：上次返回值作下次首参（WATERFALL 签名为 call(scope, effect, value)）
    rewritten = await host.hook("rewrite").call(host.new_scope(), eff, "")
    assert rewritten == "AB"

    # 3) Loop：非 None 返回则重跑整链，None 收尾
    loop_scope = host.new_scope()
    assert await host.hook("looper").call(loop_scope, eff) is None
    assert demo.attempt_n == 3

    # 4) Parallel：全部执行且异常隔离，返回 None
    demo.calls.clear()
    parallel_scope = host.new_scope()
    raise_eff = Effect(EffectKind.LLM, {"raise": True})
    assert await host.hook("observe").call(parallel_scope, raise_eff) is None
    assert "observe_a" in demo.calls and "observe_b" in demo.calls

    # 5) Around：tap 收到 next 回调，中心只执行一次（洋葱顺序）
    demo.calls.clear()

    async def core(scope: RequestScope) -> Decision:
        demo.calls.append("core")
        return Decision.continue_()

    around = await host.hook("wrap").call(host.new_scope(), eff, core=core)
    assert around is not None and around.action is DecisionAction.CONTINUE
    assert demo.calls == [
        "outer-in", "inner-in", "core", "inner-out", "outer-out",
    ]

    # 6) RequestScope：每次 new_scope 独立，信号不串联
    s1 = host.new_scope()
    s2 = host.new_scope()
    s1.emit(Signal("degrade", "boom"))
    assert s1.signals == [Signal("degrade", "boom")] and s2.signals == []

    # 7) R1 注入点：dispatch 判决 + scope 桥接供 call 阶段读取
    effect = Effect(EffectKind.LLM, {"messages": []})
    ctx = HookCtx(
        effect=effect, state=AgentState(), session_id="r2", event="before_invoke"
    )
    dispatched = await host.dispatch(effect, ctx)
    assert dispatched.action is DecisionAction.DENY

    # 健康 dispatch 保留 scope 桥接，供 call 阶段读取（R3→R4 接力）
    healthy = Host().freeze()
    healthy_ctx = HookCtx(
        effect=effect, state=AgentState(), session_id="r2-ok", event="before_invoke"
    )
    await healthy.dispatch(effect, healthy_ctx)
    assert isinstance(healthy.scope_of(healthy_ctx), RequestScope)

    print("[R2] PASS: 5 种 HookKind 语义 / define+plugin+freeze / RequestScope 隔离 / R1 注入")


asyncio.run(main())
