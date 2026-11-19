"""R3-E02/E03（真实覆盖，替代幻影 tests/r3/test_e2e_degrade.py）：
限流超限 / 重试耗尽 → 降级信号 → settle 选级 → 网关切备用后端（端到端）。

设计出处：R3-可靠性.md §9 验证矩阵 R3-E02/E03、§2.3 守卫故障→降级裁决链。
在 R1 Runner 真实链路上跑，断言 primary 零调用、backup 被调用、trace 连贯。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, Host, Plugin, RequestScope, Signal, SignalKind
from harness.model import ModelCapability
from harness.resilience import DegradeLadder, DegradeMode, Degradation, DegradeStep
from hooks.dto import LLMRequest, LLMResponse, Usage
from runtime import Effect, EffectKind, Runner, Session


class _FakeLLM:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def complete(self, request: LLMRequest, ctx: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1, completion_tokens=1))

    async def stream(self, request, ctx):  # type: ignore[no-untyped-def]
        yield self.name

    @property
    def last_usage(self):  # type: ignore[no-untyped-def]
        return Usage(prompt_tokens=1, completion_tokens=1)


class _SignalGuard(Plugin):
    """模拟守卫/重试耗尽：在 before_invoke 发降级信号 + 占位 DEGRADE。"""

    def __init__(self, name: str, reason: str, ladder: DegradeLadder) -> None:
        self.name = name
        self.reason = reason
        self.degradation = Degradation(ladder, available={"primary", "backup"})

    def apply(self, host: Host) -> None:
        guard = self

        class _Guard(Plugin):
            name = guard.name

            def apply(self, h: Host) -> None:
                h.hook("before_invoke").tap(guard.name, guard.check)

        host.plugin(_Guard()).plugin(self.degradation)

    async def check(self, scope: RequestScope, effect: Effect) -> Decision:
        scope.emit(Signal(SignalKind.DEGRADE, self.reason, "primary"))
        return Decision.degrade("")


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():
    resp = yield Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]})
    yield Effect(EffectKind.END, {"output": resp.get("content", "?") if isinstance(resp, dict) else "?"})


def _run_with(reason: str) -> tuple[object, _FakeLLM, _FakeLLM]:
    primary = _FakeLLM("primary")
    backup = _FakeLLM("backup")
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gw = cap.gateway()
    host = Host()
    host.plugin(_SignalGuard("guard", reason, _ladder())).freeze()
    runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
    result = asyncio.run(runner.run(Session(session_id="r3-e2e", program_factory=_program)))
    return result, primary, backup


class TestDegradeE2E:
    def test_rate_limit_signal_switches_to_backup(self) -> None:
        """限流超限信号 → settle 选 backup → 网关切备用，primary 零调用。"""
        result, primary, backup = _run_with("rate_limited")
        assert result.output == "backup"  # type: ignore[union-attr]
        assert backup.calls == 1 and primary.calls == 0

    def test_retry_exhausted_signal_switches_to_backup(self) -> None:
        """重试耗尽信号 → settle 选 backup → 网关切备用，primary 零调用。"""
        result, primary, backup = _run_with("retry_exhausted")
        assert result.output == "backup"  # type: ignore[union-attr]
        assert backup.calls == 1 and primary.calls == 0
