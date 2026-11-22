"""R2 独立 demo：可靠性执行器 + 领域策略 + Lifecycle 通知协同。

演示：函数式变换器的前后包裹顺序、紧急开关终止拒绝发 invoke_rejected 通知、
Lifecycle 16 通知点的订阅与隔离。不依赖外部服务，结尾输出 PASS 标记。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import Invocation, LifecycleBus, LifecyclePhase, Registry  # noqa: E402
from runtime import Invoke, Result  # noqa: E402

from harness.resilience import KillSwitch, ResilienceCapability  # noqa: E402


async def main() -> None:
    order: list[str] = []
    bus = LifecycleBus()
    seen: list[str] = []
    bus.on(LifecyclePhase.INVOKE_REJECTED, lambda n: seen.append(n.phase.value))
    reg = Registry()

    type _InvCall = Callable[[Invocation], Awaitable[Result]]

    def tracing(nxt: _InvCall) -> _InvCall:
        async def wrapped(inv: Invocation) -> Result:
            order.append("wrap:in")
            result = await nxt(inv)
            order.append("wrap:out")
            return result
        return wrapped

    async def core(inv: Invocation) -> Result:
        order.append("port")
        return Result.success({"content": "answer"})

    executor = ResilienceCapability(extra_plugins=(tracing,)).install(  # type: ignore[arg-type]
        core, registry=reg, bus=bus,
    )
    assert executor.registry is reg
    inv = Invocation(
        effect=Invoke(port="llm", payload={"messages": []}),
        ctx=type("Ctx", (), {"session_id": "demo", "meta": {}})(),
        session=type("Session", (), {"session_id": "demo"})(),
        meta={},
    )
    result = await executor.run_invocation(inv)
    assert result.ok and result.data["content"] == "answer"
    assert order == ["wrap:in", "port", "wrap:out"], order
    assert len(list(LifecyclePhase)) == 16

    # 紧急开关置位 → 终止拒绝 + invoke_rejected 通知，不调 port
    kill = KillSwitch(flag_store={"kill:global": True})
    called: list[int] = []

    async def blocked_core(inv: Invocation) -> Result:
        called.append(1)
        return Result.success("should-not-run")

    blocked = ResilienceCapability(killswitch=kill).install(blocked_core, bus=bus)
    denied = await blocked.run_invocation(inv)
    assert not denied.ok and called == []
    assert seen == ["invoke_rejected"]
    print("PASS demo_r2: 领域执行器编排顺序 + Lifecycle 通知 + 终止拒绝短路")


if __name__ == "__main__":
    asyncio.run(main())
