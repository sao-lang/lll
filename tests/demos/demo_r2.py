"""R2 独立 demo：三体系（Port + AOP Enhancer + Lifecycle hooks）协同。

演示：AOP 五角色固定顺序编排（Prelude→Warden→Envelope→Refiner→Port）、
Warden 终止类拒绝发 invoke_rejected 通知、Lifecycle 16 通知点的订阅与隔离。
不依赖外部服务，结尾输出 PASS 标记。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aop import (  # noqa: E402
    AOPContainer, Envelope, Invocation, Prelude, Refiner, Verdict, Warden,
)
from runtime import Result  # noqa: E402
from hooks import LifecycleBus, LifecyclePhase, Notice, Registry  # noqa: E402
from runtime import Invoke  # noqa: E402


class TracingPrelude(Prelude):
    name = "demo_prelude"
    def __init__(self, order: list[str]) -> None:
        self.order = order
    async def run(self, inv: Invocation, next) -> Result:
        self.order.append("prelude:in")
        r = await next(inv)
        self.order.append("prelude:out")
        return r


class AllowWarden(Warden):
    name = "demo_warden"
    async def guard(self, inv: Invocation) -> Verdict:
        return Verdict(allowed=True, reason="ok")


class TimingEnvelope(Envelope):
    name = "demo_envelope"
    async def wrap(self, inv: Invocation, next) -> Result:
        return await next(inv)


class NoopRefiner(Refiner):
    name = "demo_refiner"
    async def refine(self, inv: Invocation) -> Invocation:
        return inv


async def main() -> None:
    order: list[str] = []
    bus = LifecycleBus()
    seen: list[str] = []
    bus.on(LifecyclePhase.INVOKE_REJECTED, lambda n: seen.append(n.phase.value))

    async def core(inv: Invocation) -> Result:
        order.append("port")
        return Result.success({"content": "answer"})

    container = (AOPContainer(core, registry=Registry(), bus=bus)
                 .add_prelude(TracingPrelude(order))
                 .add_warden(AllowWarden())
                 .add_envelope(TimingEnvelope())
                 .add_refiner(NoopRefiner()))
    inv = Invocation(
        effect=Invoke(port="llm", payload={"messages": []}),
        ctx=type("Ctx", (), {"session_id": "demo", "meta": {}})(),
        session=type("Session", (), {"session_id": "demo"})(),
        meta={},
    )
    result = await container.run_invocation(inv)
    assert result.ok and result.data["content"] == "answer"
    assert order == ["prelude:in", "port", "prelude:out"], order
    assert len(list(LifecyclePhase)) == 16

    # 终止类拒绝 → invoke_rejected 通知 + 不调 port
    class Block(Warden):
        name = "block"
        async def guard(self, i): return Verdict(allowed=False, reason="blocked")
    blocked = AOPContainer(core, bus=bus).add_warden(Block())
    denied = await blocked.run_invocation(inv)
    assert not denied.ok and seen == ["invoke_rejected"]
    print("PASS demo_r2: AOP 五角色顺序 + Lifecycle 通知 + 拒绝短路")


if __name__ == "__main__":
    asyncio.run(main())
