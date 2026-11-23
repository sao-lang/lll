"""R3 拆分四坑回归（规格 P3 门禁红线）：断言逐字复用现有端到端用例，只换宿主到组合层。

四坑（P9）：B1 按 session_id 隔离；B2 failed_target 是后端注册名；R3-6 available
永久剔除写回；R3-7 retry/timeout 故障记录带 failed_target。**断言不放水**——
与 tests/r3/test_degrade.py::TestDegradationSelection 的断言逐字相同，只把
宿主换成可靠性执行器显式编排的领域策略（CallState/FaultRecord/RoutingTarget 为 D-3 通用原语）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from runtime import Invoke

from harness.resilience import (
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    Degradation,
    RateLimit,
    ResilienceCapability,
)
from compose.context import CallContext
from compose.guards import (
    CallState,
    FaultRecord,
    RoutingTarget,
)

SID = "s-degrade"


@dataclass
class _Ctx:
    """最小 ctx：模拟 R1 RuntimeCtx.meta（含 session_id），供 CallState 读写。"""
    session_id: str = SID
    meta: dict[str, Any] = field(default_factory=dict)


def _ladder() -> DegradeLadder:
    """测试降级阶梯：主→备→拒答（与 tests/r3 一致）。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _fault(reason: str, target: str) -> FaultRecord:
    """构造具名故障记录。"""
    return FaultRecord(reason=reason, failed_target=target)


class TestFourPitsOnExecutorHost:
    """四坑语义在领域执行器宿主上逐条复现（断言不放水）。"""

    def test_select_shrinks_available_step_by_step(self) -> None:
        """B2/R3-6：按失败目标逐级永久剔除，全剔除落 REFUSE。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.select(FaultRecord("none", ""), session_id=SID) == "primary"
        assert plugin.select(_fault("primary_down", "primary"), session_id=SID) == "backup"
        assert plugin.select(FaultRecord("none", ""), session_id=SID) == "backup"
        assert plugin.select(_fault("backup_down", "backup"), session_id=SID) == DegradeMode.REFUSE.value

    def test_r3_6_shrink_is_permanent_not_local_copy(self) -> None:
        """R3-6：剔除写回实例 available——primary 故障后不会在下一轮被重新选回。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        plugin.select(_fault("primary_down", "primary"), session_id=SID)
        for _ in range(3):
            assert plugin.select(FaultRecord("none", ""), session_id=SID) == "backup"

    def test_b1_session_isolation(self) -> None:
        """B1：按 session_id 隔离目标状态；A 会话剔除 primary 不影响 B 会话。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.select(_fault("primary_down", "primary"), session_id="session-A") == "backup"
        assert plugin.select(FaultRecord("none", ""), session_id="session-B") == "primary"
        assert "session-A" in plugin._session_state and "session-B" in plugin._session_state

    def test_b2_failed_target_is_registration_name(self) -> None:
        """B2：failed_target 由真实 RateLimit 许可产出，是阶梯后端注册名（非手工自证）。"""
        import asyncio
        from runtime import Result
        from harness.resilience import RateLimit, RatePermit, ResilienceCapability

        # 真实限流插件在 on_exceed="degrade" 时返回 DEGRADE，执行器写带注册名的 fault
        rl = RateLimit(rate=60, burst=1, on_exceed="degrade", clock=lambda: 0.0)
        eff = Invoke(port="llm", payload={"model": "primary"})
        ctx = CallContext(effect=eff, ctx=object(),
                          session=type("S", (), {"session_id": SID})(), meta={})
        assert rl.acquire() is RatePermit.ADMIT  # 耗尽令牌
        assert rl.acquire() is RatePermit.DEGRADE  # 超限 → 降级

        async def ok_core(inv: CallContext) -> Result:
            return Result.success("ok")

        executor = ResilienceCapability(rate_limit=rl).wrap(ok_core)
        result = asyncio.run(executor.run_invocation(ctx))
        assert result.ok  # 降级类放行（与 tests/r3/test_degrade.py 一致）
        fault = ctx.fault()
        assert fault is not None and fault.reason == "rate_limited"
        assert fault.failed_target == "primary"  # B2：注册名（与 tests/r3 逐字一致）


class TestCallStateTypedTarget:
    """typed 目标载体：执行器选级写入 ctx.meta、网关消费（替代旧 Invocation.meta 裸键）。"""

    def test_write_read_routing_target(self) -> None:
        """限流写 fault → select 选 backup → typed target 写 ctx.meta 供网关消费。"""
        import asyncio
        from runtime import Result

        limit = RateLimit(rate=0.0, burst=0, on_exceed="degrade", clock=lambda: 0.0)
        degradation = Degradation(_ladder(), available={"primary", "backup"})
        calls: list[str] = []

        async def core(inv: CallContext) -> Result:
            rt = inv.routing_target()
            target = rt.target if rt else "primary"
            calls.append(target)
            return Result.success(target)

        executor = ResilienceCapability(rate_limit=limit, degradation=degradation).wrap(core)
        meta: dict = {}
        ctx = _Ctx(meta=meta)
        inv = CallContext(
            effect=Invoke(port="llm", payload={"model": "primary"}),
            ctx=ctx, session=type("S", (), {"session_id": SID})(), meta=meta,
        )
        result = asyncio.run(executor.run_invocation(inv))
        # CallState 从 ctx.meta 读回：与网关消费同一份 meta（同源）
        target = CallState(ctx).routing_target()
        assert result.ok
        assert target is not None and target.target == "backup"
        assert target.reason == "rate_limited"
        assert calls == ["backup"]  # 网关按 typed target 切 backup，primary 零调用

    def test_refuse_is_typed_not_fabricated(self) -> None:
        """全剔除 → refuse typed 目标，reason 只进 trace 不作 target（不编造答案）。"""
        import asyncio
        from runtime import Result

        limit = RateLimit(rate=0.0, burst=0, on_exceed="degrade", clock=lambda: 0.0)
        degradation = Degradation(_ladder(), available={"backup"})  # 仅 backup 可用
        called: list[str] = []

        async def core(inv: CallContext) -> Result:
            called.append("core")
            return Result.success("fabricated")

        executor = ResilienceCapability(rate_limit=limit, degradation=degradation).wrap(core)
        # failed_target 由真实 RateLimit 许可派生为注册名 "backup"（B2，非手工自证）
        meta: dict = {}
        inv = CallContext(
            effect=Invoke(port="llm", payload={"model": "backup"}),
            ctx=_Ctx(meta=meta), session=type("S", (), {"session_id": SID})(), meta=meta,
        )
        result = asyncio.run(executor.run_invocation(inv))
        assert not result.ok and result.error is not None and result.error.code == "REFUSED"
        assert called == []  # 全剔除后真拒答，零后端调用、不编造答案
        target = CallState(inv).routing_target()
        assert target is not None and target.target == DegradeMode.REFUSE.value
        assert target.reason == "rate_limited" and target.reason != target.target  # reason 只进 trace


class TestR37RealRetrySource:
    """R3-7 真实故障源：RetryPolicy 耗尽写带 failed_target 的 fault → 降级 → 切 backup。

    断言逐字复用 tests/r3/test_e2e_degrade.py:133-138（primary.calls==2 / backup.calls==1），
    只把宿主换成领域执行器 + ResilienceCapability.install。
    """

    def test_real_retry_exhaustion_switches_to_backup(self) -> None:
        """重试真的耗尽 → fault 带 failed_target → 选 backup → 切备用，断言不放水。"""
        import asyncio
        from runtime import Result
        from harness.resilience import RetryPolicy, ResilienceCapability

        class _LLM:
            def __init__(self, name: str, fail: bool = False) -> None:
                self.name, self.calls, self.fail = name, 0, fail

            async def complete(self, request: object, ctx: object) -> object:
                self.calls += 1
                from compose import LLMResponse, Usage
                if self.fail:
                    return LLMResponse(content="", error="500:backend down", usage=Usage())
                return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1))

        primary = _LLM("primary", fail=True)
        backup = _LLM("backup")

        async def core(inv: CallContext) -> Result:
            """最小 core：从 typed target 选后端，模拟网关消费（与 e2e 用例同构）。"""
            rt = inv.routing_target()
            target = rt.target if rt else "primary"
            resp = await (primary if target == "primary" else backup).complete(None, None)
            error = getattr(resp, "error", "")
            if error:
                return Result.failure(error.split(":")[0], error)
            return Result.success(getattr(resp, "content", ""))

        retry = RetryPolicy(max_attempts=2, base_delay=0.001, jitter="none")
        degradation = Degradation(_ladder(), available={"primary", "backup"})
        executor = ResilienceCapability(retry=retry, degradation=degradation).wrap(core)
        inv = CallContext(
            effect=Invoke(port="llm", payload={"model": "primary"}),
            ctx=object(), session=type("S", (), {"session_id": "r37"})(), meta={},
        )
        result = asyncio.run(executor.run_invocation(inv))
        # 以下断言逐字复用 test_e2e_degrade.py:136-138（不放水）
        assert result.data == "backup"
        assert primary.calls == 2, f"primary 应被重试 2 次，实际 {primary.calls}"
        assert backup.calls == 1, "backup 应被调用 1 次（重路由）"
