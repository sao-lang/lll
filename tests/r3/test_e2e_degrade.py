"""R3-E02/E03 端到端降级（新体系）：Warden 写 fault → Degradation 选级 → typed target → 网关切 backup。

在 R1 Runner 真实链路上跑，断言 primary 零调用、backup 被调用、output 正确（断言不放水）。
"""
from __future__ import annotations

import asyncio

from compose.context import FaultRecord, Invocation, Verdict

from compose import AOPContainer, Warden
from runtime import Invoke, Result, Runner, Session, Terminate

from harness.model import ModelCapability
from harness.resilience import DegradeLadder, DegradeMode, Degradation, DegradeStep


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


class _FaultWarden(Warden):
    """模拟限流超限 / 重试耗尽：写具名故障记录（含 failed_target，B2）并放行。"""
    name = "fault_warden"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def guard(self, inv: Invocation) -> Verdict:
        inv.write_fault(FaultRecord(reason=self.reason, failed_target="primary"))
        return Verdict(allowed=True)  # 降级类放行，容器据此重路由


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():
    resp = yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Terminate(output=resp.get("content", "?") if isinstance(resp, dict) else "?")


def _run_with(reason: str) -> tuple[object, _FakeLLM2, _FakeLLM2]:
    primary = _FakeLLM2("primary")
    backup = _FakeLLM2("backup")
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gw = cap.gateway()

    degradation = Degradation(_ladder(), available={"primary", "backup"})
    container = AOPContainer(gw.as_call()).add_warden(_FaultWarden(reason)).add_salvage(degradation)
    runner = Runner(call=container.as_call())
    result = asyncio.run(runner.run(Session(session_id="r3-e2e", program_factory=_program)))
    return result, primary, backup


class TestDegradeE2E:
    def test_rate_limit_fault_switches_to_backup(self) -> None:
        """限流故障 → Degradation 选 backup → 网关切备用，primary 零调用。"""
        result, primary, backup = _run_with("rate_limited")
        assert result.output == "backup"  # type: ignore[union-attr]
        assert backup.calls == 1 and primary.calls == 0

    def test_retry_exhausted_fault_switches_to_backup(self) -> None:
        """重试耗尽故障 → Degradation 选 backup → 网关切备用，primary 零调用。"""
        result, primary, backup = _run_with("retry_exhausted")
        assert result.output == "backup"  # type: ignore[union-attr]
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
    """真实 RetryPolicy 故障源：primary 恒失败 → retry 耗尽写 fault → Degradation 选 backup。"""
    from harness.resilience import RetryPolicy
    primary = _ErrorLLM("primary", fail=True)
    backup = _ErrorLLM("backup")
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gw = cap.gateway()
    retry = RetryPolicy(max_attempts=2, base_delay=0.001, jitter="none")
    degradation = Degradation(_ladder(), available={"primary", "backup"})
    container = (AOPContainer(gw.as_call())
                 .add_envelope(retry)  # 真实 Envelope：耗尽后写 fault
                 .add_salvage(degradation))
    runner = Runner(call=container.as_call())
    result = asyncio.run(runner.run(Session(session_id="r3-real-retry", program_factory=_program)))
    return result, primary, backup


class TestDegradeE2ERealEnvelope:
    """真实 RetryPolicy/Timeout 故障源（非 Warden 模拟）——P9 R3-7 回归门禁。"""

    def test_real_retry_exhaustion_switches_to_backup(self) -> None:
        """重试真的耗尽 → fault 带 failed_target → Degradation 选 backup → 网关切备用。"""
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
        from harness.resilience import RetryPolicy
        retry = RetryPolicy(max_attempts=1, base_delay=0.001, jitter="none")
        degradation = Degradation(_ladder(), available={"primary", "backup"})
        container = (AOPContainer(cap.gateway().as_call())
                     .add_envelope(retry).add_salvage(degradation))
        runner = Runner(call=container.as_call())
        result = asyncio.run(runner.run(Session(session_id="r3-real-refuse", program_factory=_program)))
        first = result.events[0].results[0]
        assert not first.ok and first.error is not None and first.error.code == "REFUSED"
        assert primary.calls == 1 and backup.calls == 1, "两个后端各调一次后拒答，绝不静默回退"


def test_degrade_with_stateful_warden_not_self_blocked() -> None:
    """幂等等状态型 Warden + 降级：重路由重跑不得二次拦截（复审 Should-新1 回归）。"""
    from harness.resilience import Idempotency, RetryPolicy
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

    container = (AOPContainer(core)
                 .add_warden(idem)
                 .add_envelope(retry)
                 .add_salvage(deg))
    inv = Invocation(
        effect=Invoke(port="llm", payload={"model": "primary"}, idempotency_key="K-DEGRADE"),
        ctx=type("Ctx", (), {"session_id": "s", "meta": {}})(),
        session=type("S", (), {"session_id": "s"})(),
        meta={},
    )
    result = asyncio.run(container.run_invocation(inv))
    assert result.ok and result.data == "backup-ok"
    assert calls == ["primary", "backup"], f"重路由不得被幂等自命中，实际 {calls}"
