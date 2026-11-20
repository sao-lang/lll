"""AOP 增强体系契约：五角色固定顺序 + 拒绝短路 + 降级重路由 + 共用 Registry。"""
from __future__ import annotations

import asyncio

from compose import (
    AOPContainer,
    Envelope,
    FaultRecord,
    Invocation,
    Prelude,
    Refiner,
    RoutingTarget,
    Verdict,
    Warden,
)
from compose import LifecycleBus, LifecyclePhase, Notice, Registry
from runtime import Invoke, Result


class _SessionView:
    session_id = "s-aop"


def _inv(port: str = "llm", meta: dict | None = None) -> Invocation:
    return Invocation(
        effect=Invoke(port=port, payload={}),
        ctx=type("Ctx", (), {"session_id": "s-aop"})(),
        session=_SessionView(),
        meta=meta or {},
    )


def test_fixed_order_prelude_warden_envelope_refiner_core():
    """执行顺序固定：Prelude→Warden→Envelope前→Refiner→core→Envelope后。"""
    order: list[str] = []

    class P(Prelude):
        name = "p"
        async def run(self, inv, next):
            order.append("prelude:before")
            r = await next(inv)
            order.append("prelude:after")
            return r

    class W(Warden):
        name = "w"
        async def guard(self, inv):
            order.append("warden")
            return Verdict(allowed=True)

    class E(Envelope):
        name = "e"
        async def wrap(self, inv, next):
            order.append("envelope:before")
            r = await next(inv)
            order.append("envelope:after")
            return r

    class R(Refiner):
        name = "r"
        async def refine(self, inv):
            order.append("refiner")
            return inv

    async def core(inv):
        order.append("core")
        return Result.success("ok")

    c = AOPContainer(core).add_prelude(P()).add_warden(W()).add_envelope(E()).add_refiner(R())
    result = asyncio.run(c.run_invocation(_inv()))
    assert result.ok and result.data == "ok"
    assert order == [
        "prelude:before", "warden", "envelope:before",
        "refiner", "core", "envelope:after", "prelude:after",
    ]


def test_warden_deny_short_circuits_and_emits_rejected():
    """终止类拒绝：不调 core，发 invoke_rejected 通知。"""
    rejected: list[Notice] = []
    bus = LifecycleBus()
    bus.on(LifecyclePhase.INVOKE_REJECTED, lambda n: rejected.append(n))

    class Block(Warden):
        name = "block"
        async def guard(self, inv):
            return Verdict(allowed=False, reason="blocked-by-policy")

    called = []
    async def core(inv):
        called.append(1)
        return Result.success("should-not-run")

    c = AOPContainer(core, bus=bus).add_warden(Block())
    result = asyncio.run(c.run_invocation(_inv()))
    assert not result.ok
    assert called == []
    assert len(rejected) == 1 and rejected[0].payload["reason"] == "blocked-by-policy"


def test_envelope_retry_calls_core_multiple_times():
    """Envelope 包裹重试：next 可调多次，失败重试后成功。"""
    class Retry(Envelope):
        name = "retry"
        def __init__(self): self.n = 0
        async def wrap(self, inv, next):
            for _ in range(3):
                self.n += 1
                r = await next(inv)
                if r.ok:
                    return r
            return r

    attempts = []
    async def core(inv):
        attempts.append(1)
        return Result.success("ok") if len(attempts) >= 2 else Result.failure("TRANSIENT", "t")

    c = AOPContainer(core).add_envelope(Retry())
    result = asyncio.run(c.run_invocation(_inv()))
    assert result.ok and len(attempts) == 2


def test_meta_is_single_context_for_fault_and_routing():
    """上下文单一：Warden 写 fault、Salvage 读 fault 写 routing_target，共享同一 meta。"""
    class FaultWarden(Warden):
        name = "fw"
        async def guard(self, inv):
            inv.write_fault(FaultRecord(reason="rate_limited", failed_target="primary"))
            return Verdict(allowed=True)  # 降级类放行

    class DegradeSalvage:
        name = "deg"
        async def recover(self, exc, inv):
            fault = inv.fault()
            assert fault is not None and fault.failed_target == "primary"
            inv.write_routing_target(RoutingTarget(target="backup", reason=fault.reason))
            return Result.success("backup")

    seen = []
    async def core(inv):
        seen.append(inv.routing_target().target if inv.routing_target() else None)
        return Result.success("backup-answer")

    c = AOPContainer(core).add_warden(FaultWarden()).add_salvage(DegradeSalvage())  # type: ignore[arg-type]
    inv = _inv()
    result = asyncio.run(c.run_invocation(inv))
    assert seen == ["backup"]
    assert inv.routing_target() == RoutingTarget(target="backup", reason="rate_limited")
    assert result.ok


def test_registry_is_shared_reference():
    """共用 Registry：容器持有同一实例引用（P6 两表统一）。"""
    reg = Registry()
    c = AOPContainer(lambda inv: Result.success("x"), registry=reg)
    assert c.registry is reg


def test_salvage_path_emits_invoke_rejected():
    """异常/降级兜底也强制发 invoke_rejected（堵审计盲区，规格 P3）。"""
    rejected: list[Notice] = []
    bus = LifecycleBus()
    bus.on(LifecyclePhase.INVOKE_REJECTED, lambda n: rejected.append(n))

    class DegradeSalvage:
        name = "deg"
        async def recover(self, exc, inv):
            inv.write_routing_target(RoutingTarget("backup", "degraded"))
            return Result.success("backup")

    # core 抛异常 → Salvage 兜底 → 必发 invoke_rejected
    async def boom(inv):
        raise RuntimeError("port crashed")

    c = AOPContainer(boom, bus=bus).add_salvage(DegradeSalvage())  # type: ignore[arg-type]
    result = asyncio.run(c.run_invocation(_inv()))
    assert result.ok
    assert len(rejected) == 1
    assert "port crashed" in rejected[0].payload["reason"]
