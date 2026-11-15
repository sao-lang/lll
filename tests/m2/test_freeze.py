"""M2-T07 装配期检查 / 透明性红线 / middleware 短路与超时。"""
from __future__ import annotations

import asyncio

import pytest

from agentkit.chains import Chains, ChainConfigError, ChainCtx, MountPoint
from agentkit.chains.builtins import (
    AllowFilter, NeedsPortMiddleware, PassMiddleware, ShortCircuitMiddleware,
    TimeoutMiddleware,
)
from agentkit.runtime import AgentState, Effect, EffectKind, Result
from tests.fakes import run


def ctx(invoke=None) -> ChainCtx:
    c = ChainCtx(Effect(EffectKind.LLM, {"messages": []}), AgentState(), "freeze")
    c.invoke = invoke or (lambda e, x: Result.success("ok"))
    return c


def test_t07_illegal_mount_point_rejected():
    with pytest.raises(ChainConfigError):
        Chains().filter(AllowFilter(), at="not-a-mount")


def test_t07_missing_port_detected_at_freeze():
    chains = Chains().use(NeedsPortMiddleware())
    with pytest.raises(ChainConfigError, match="requires port"):
        chains.freeze(required_ports=[])
    assert chains.freeze(required_ports=("llm",)).required_ports == frozenset({"llm"})


def test_t07_freeze_locks_registry():
    chains = Chains().filter(AllowFilter(), at=MountPoint.BEFORE_LLM).freeze()
    with pytest.raises(ChainConfigError):
        chains.hook(AllowFilter())


def test_t07_undeclared_short_circuit_is_rejected():
    chains = Chains().use(ShortCircuitMiddleware())
    out = run(chains.dispatch(ctx().effect, ctx()))
    assert out.data.error.code == "MW_SHORT_CIRCUIT"
    assert "middleware:short-circuit-mw:undeclared-short-circuit" in out.trace


def test_t07_declared_short_circuit_is_allowed():
    chains = Chains().use(ShortCircuitMiddleware(), allow_short_circuit=True)
    out = run(chains.dispatch(ctx().effect, ctx()))
    assert out.data.ok and out.data.data == "short-circuited"


def test_t07_timeout_middleware():
    async def slow(effect, c):
        await asyncio.sleep(0.05)
        return Result.success("late")

    chains = Chains().use(TimeoutMiddleware("timeout", 0.001))
    c = ctx(slow)
    out = run(chains.dispatch(c.effect, c))
    # 超时异常由 Runner 层折叠；链路内中间件保持洋葱语义
    assert out.data.error.code == "MIDDLEWARE_ERROR"
    assert "middleware:timeout:error:TimeoutError" in out.trace


def test_t07_pass_middleware_forwards_inner_result():
    out = run(Chains().use(PassMiddleware()).dispatch(ctx().effect, ctx()))
    assert out.data.ok and out.data.data == "ok"


def test_t07_invoke_missing_is_reported():
    c = ctx()
    c.invoke = None
    out = run(Chains().dispatch(c.effect, c))
    assert out.data.error.code == "INVOKE_MISSING"
