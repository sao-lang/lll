"""R3-E02/E03 端到端降级（领域执行器）：故障 → 降级选级 → typed target → 网关切 backup。

在 R1 Runner 真实链路上跑，断言 primary 零调用、backup 被调用、output 正确（断言不放水）。
故障源两种：函数式变换器写 fault（模拟限流/重试耗尽），或真实重试策略耗尽。
"""
from __future__ import annotations

import asyncio

from compose.context import Invocation

from runtime import Invoke, Result, Runner, Session, Terminate

from harness.model import ModelCapability
from harness.resilience import (
    DegradeLadder,
    DegradeMode,
    Degradation,
    DegradeStep,
    RateLimit,
    ResilienceCapability,
    RetryPolicy,
)


class _FakeLLM2:
    """最小 LLMPort fake：记录调用次数，返回自身名作 content。"""
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def complete(self, request, ctx):
        self.calls += 1
        from compose import LLMResponse, Usage
        return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1, completion_tokens=1))

    async def stream(self, request, ctx):
        yield self.name

    @property
    def last_usage(self):
        from compose import Usage
        return Usage(prompt_tokens=1, completion_tokens=1)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():
    resp = yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Terminate(output=resp.get("content", "?") if isinstance(resp, dict) else "?")


def _run_with_limit(limit: RateLimit) -> tuple[object, _FakeLLM2, _FakeLLM2]:
    primary = _FakeLLM2("primary")
    backup = _FakeLLM2("backup")
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gw = cap.gateway()

    degradation = Degradation(_ladder(), available={"primary", "backup"})
    reliability = ResilienceCapability(
        rate_limit=limit, degradation=degradation,
    ).wrap(gw.as_call())
    runner = Runner(call=reliability.as_call())
    result = asyncio.run(runner.run(Session(session_id="r3-e2e", program_factory=_program)))
    return result, primary, backup


class TestDegradeE2E:
    def test_rate_limit_degrade_switches_to_backup(self) -> None:
        """限流超限（DEGRADE）→ 降级选级选 backup → 网关切备用，primary 零调用。"""
        limit = RateLimit(rate=0.0, burst=0, on_exceed="degrade", clock=lambda: 0.0)
        result, primary, backup = _run_with_limit(limit)
        assert result.output == "backup"  # type: ignore[union-attr]
        assert backup.calls == 1 and primary.calls == 0

    def test_rate_limit_degrade_reports_fault_reason(self) -> None:
        """限流降级写具名 fault（rate_limited）→ typed target 带同名 reason。"""
        limit = RateLimit(rate=0.0, burst=0, on_exceed="degrade", clock=lambda: 0.0)
        degradation = Degradation(_ladder(), available={"primary", "backup"})
        primary = _FakeLLM2("primary")
        backup = _FakeLLM2("backup")
        cap = ModelCapability(llm=primary)
        cap.add_backend("backup", backup)
        reliability = ResilienceCapability(rate_limit=limit, degradation=degradation).wrap(
            cap.gateway().as_call())
        meta: dict = {}
        ctx = type("Ctx", (), {"session_id": "s", "meta": meta})()
        inv = Invocation(
            effect=Invoke(port="llm", payload={"model": "primary"}),
            ctx=ctx, session=type("S", (), {"session_id": "s"})(), meta=meta,
        )
        # 直接跑 preflight 之后看不到 meta 共享（ctx 与 inv.meta 同源需经 as_call）；
        # 用 run_invocation 走完整链，断言切备用且 primary 零调用。
        result = asyncio.run(reliability.run_invocation(inv))
        target = inv.routing_target()
        assert result.ok and target is not None and target.target == "backup"
        assert target.reason == "rate_limited", f"typed target 应带限流原因，实际 {target.reason}"
        assert backup.calls == 1 and primary.calls == 0


class _ErrorLLM:
    """返回可重试失败响应的后端（模拟 primary 宕机，触发真实 RetryPolicy 耗尽）。"""
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.calls = 0
        self.fail = fail

    async def complete(self, request, ctx):
        self.calls += 1
        from compose import LLMResponse, Usage
        if self.fail:
            return LLMResponse(content="", error="500:backend down", usage=Usage())
        return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1))

    async def stream(self, request, ctx):
        yield self.name

    @property
    def last_usage(self):
        from compose import Usage
        return Usage(prompt_tokens=1)


def _run_with_real_retry() -> tuple[object, _ErrorLLM, _ErrorLLM]:
    """真实 RetryPolicy 故障源：primary 恒失败 → retry 耗尽写 fault → 降级选 backup。"""
    primary = _ErrorLLM("primary", fail=True)
    backup = _ErrorLLM("backup")
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gw = cap.gateway()
    retry = RetryPolicy(max_attempts=2, base_delay=0.001, jitter="none")
    degradation = Degradation(_ladder(), available={"primary", "backup"})
    reliability = ResilienceCapability(
        retry=retry, degradation=degradation,
    ).wrap(gw.as_call())
    runner = Runner(call=reliability.as_call())
    result = asyncio.run(runner.run(Session(session_id="r3-real-retry", program_factory=_program)))
    return result, primary, backup


class TestDegradeE2ERealRetry:
    """真实 RetryPolicy 故障源（非写 fault 模拟）——P9 R3-7 回归门禁。"""

    def test_real_retry_exhaustion_switches_to_backup(self) -> None:
        """重试真的耗尽 → fault 带 failed_target → 降级选 backup → 网关切备用。"""
        result, primary, backup = _run_with_real_retry()
        assert result.output == "backup"  # type: ignore[union-attr]
        assert primary.calls == 2, f"primary 应被重试 2 次，实际 {primary.calls}"
        assert backup.calls == 1, "backup 应被调用 1 次（重路由）"

    def test_real_retry_all_down_refuses(self) -> None:
        """primary/backup 全故障 → 逐级剔除 → REFUSE 真拒答，零后端成功调用。"""
        primary = _ErrorLLM("primary", fail=True)
        backup = _ErrorLLM("backup", fail=True)
        cap = ModelCapability(llm=primary)
        cap.add_backend("backup", backup)
        retry = RetryPolicy(max_attempts=1, base_delay=0.001, jitter="none")
        degradation = Degradation(_ladder(), available={"primary", "backup"})
        reliability = ResilienceCapability(
            retry=retry, degradation=degradation,
        ).wrap(cap.gateway().as_call())
        runner = Runner(call=reliability.as_call())
        result = asyncio.run(runner.run(Session(session_id="r3-real-refuse", program_factory=_program)))
        first = result.events[0].results[0]
        assert not first.ok and first.error is not None and first.error.code == "REFUSED"
        assert primary.calls == 1 and backup.calls == 1, "两个后端各调一次后拒答，绝不静默回退"


def test_core_exception_degrades_to_backup() -> None:
    """中心调用抛异常（非 Result 失败）→ 折叠 fault → 选级切 backup（行为等价旧容器）。"""
    from compose.context import Invocation as Inv

    primary_calls: list[str] = []
    backup_calls: list[str] = []

    async def core(inv: Inv) -> Result:
        rt = inv.routing_target()
        target = rt.target if rt else "primary"
        if target == "primary":
            primary_calls.append(target)
            raise RuntimeError("primary crashed")
        backup_calls.append(target)
        return Result.success("backup-ok")

    degradation = Degradation(_ladder(), available={"primary", "backup"})
    reliability = ResilienceCapability(degradation=degradation).wrap(core)
    inv = Invocation(
        effect=Invoke(port="llm", payload={"model": "primary"}),
        ctx=type("Ctx", (), {"session_id": "s", "meta": {}})(),
        session=type("S", (), {"session_id": "s"})(),
        meta={},
    )
    result = asyncio.run(reliability.run_invocation(inv))
    assert result.ok and result.data == "backup-ok"  # type: ignore[union-attr]
    assert len(primary_calls) == 1 and backup_calls == ["backup"]


def test_degrade_with_idempotency_not_self_blocked() -> None:
    """幂等策略 + 降级：重路由重跑不得二次拦截（复审回归）。"""
    from harness.resilience import Idempotency
    idem = Idempotency()
    deg = Degradation(_ladder(), available={"primary", "backup"})
    retry = RetryPolicy(max_attempts=1, base_delay=0.001, jitter="none")
    calls: list[str] = []

    async def core(inv):
        rt = inv.routing_target()
        target = rt.target if rt else "primary"
        calls.append(target)
        if target == "backup":
            return Result.success("backup-ok")
        return Result.failure("500", f"{target} down")

    reliability = ResilienceCapability(
        idempotency=idem, retry=retry, degradation=deg,
    ).wrap(core)
    inv = Invocation(
        effect=Invoke(port="llm", payload={"model": "primary"}, idempotency_key="K-DEGRADE"),
        ctx=type("Ctx", (), {"session_id": "s", "meta": {}})(),
        session=type("S", (), {"session_id": "s"})(),
        meta={},
    )
    result = asyncio.run(reliability.run_invocation(inv))
    assert result.ok and result.data == "backup-ok"
    assert calls == ["primary", "backup"], f"重路由不得被幂等自命中，实际 {calls}"
