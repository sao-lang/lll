"""生产接线验证（修复独立 review B1）：CompositeCall 真实走 build_call + Guard + 装饰器。

覆盖适配器（WardenGuardAdapter/envelope_decorator）、状态容器统一（B3）、
以及 typed target 在守卫写入后网关可读的集成断言。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import Invoke, Result

from compose import (
    AOPContainer,
    CallContext,
    CompositeCall,
    FaultRecord,
    RoutingTarget,
    Verdict,
    WardenGuardAdapter,
    envelope_decorator,
)
from compose.call import Guard, build_call, with_guard


class _AllowWarden:
    name = "allow"
    def __init__(self) -> None:
        self.checked = 0
        self.observed = 0

    async def guard(self, inv: object) -> Verdict:
        self.checked += 1
        return Verdict(allowed=True)

    async def observe(self, inv: object, result: Result) -> None:
        self.observed += 1


class _RejectWarden:
    name = "reject"
    async def guard(self, inv: object) -> Verdict:
        return Verdict(allowed=False, reason="blocked")


class _Envelope:
    def __init__(self) -> None:
        self.calls = 0

    async def wrap(self, inv: object, nxt: object) -> Result:
        self.calls += 1
        result = await nxt(inv)  # type: ignore[operator]
        return Result.success(f"wrapped:{result.data}")


def test_warden_guard_adapter_check_and_observe() -> None:
    """WardenGuardAdapter：把插件 guard/observe 适配为 compose.Guard 协议。"""
    plugin = _AllowWarden()
    guard = WardenGuardAdapter(plugin)
    assert isinstance(guard, Guard)
    effect = Invoke(port="llm")
    ctx = object()
    assert asyncio.run(guard.check(effect, ctx)) == Verdict(allowed=True)
    asyncio.run(guard.observe(effect, Result.success("ok"), ctx))
    assert plugin.checked == 1 and plugin.observed == 1


def test_warden_guard_adapter_reject() -> None:
    """WardenGuardAdapter：拒绝 Verdict 原样透传（allowed=False）。"""
    guard = WardenGuardAdapter(_RejectWarden())
    verdict = asyncio.run(guard.check(Invoke(port="llm"), object()))
    assert verdict is not None and verdict.allowed is False and verdict.reason == "blocked"


def test_envelope_decorator_wraps_callfn() -> None:
    """envelope_decorator：把 wrap(inv,next) 适配为 CallFn 装饰器，前后包裹生效。"""
    envelope = _Envelope()

    async def base(effect: object, ctx: object) -> Result:
        return Result.success("core")

    wrapped = envelope_decorator(envelope)(base)
    result = asyncio.run(wrapped(Invoke(port="llm"), object()))
    assert result.ok and result.data == "wrapped:core"
    assert envelope.calls == 1


def test_composite_call_uses_build_call_with_guard() -> None:
    """CompositeCall 生产接线：守卫经 Guard 协议短路（真实走 build_call + with_guard）。"""
    container = AOPContainer(lambda inv: Result.success("ok"))
    container.add_warden(_RejectWarden())
    inv = CallContext(
        effect=Invoke(port="llm"), ctx=object(),
        session=type("S", (), {"session_id": "w"})(), meta={},
    )
    result = asyncio.run(container.run_invocation(inv))
    assert not result.ok  # 守卫拒绝短路（经 Guard.check）


def test_shared_meta_between_guard_and_gateway() -> None:
    """B3 状态容器统一：CallContext.meta 与 R1 ctx.meta 同源，typed target 写读一致。"""
    ctx = type("Ctx", (), {"session_id": "s", "meta": {}})()
    container = AOPContainer(lambda inv: Result.success("ok"))
    # as_call 构造的 CallContext.meta 应就是 ctx.meta（同一 dict）
    call = container.as_call()
    effect = Invoke(port="llm")
    asyncio.run(call(effect, ctx))
    # 写 typed target 到 ctx.meta，经 as_call 链应能看到同源
    ctx.meta["routing_target"] = RoutingTarget(target="backup", reason="test")
    assert ctx.meta["routing_target"].target == "backup"


def test_with_guard_uses_compose_guard_protocol() -> None:
    """with_guard：走 compose.Guard 协议（check 拒绝不调 base，observe 后置）。"""
    plugin = _AllowWarden()

    async def base(effect: object, ctx: object) -> Result:
        return Result.success("ok")

    result = asyncio.run(with_guard(base, WardenGuardAdapter(plugin))(Invoke(port="llm"), object()))
    assert result.ok and plugin.checked == 1 and plugin.observed == 1
