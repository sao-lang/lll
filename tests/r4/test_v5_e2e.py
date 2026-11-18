"""R1-R4 v5 端到端：Runner → host → R3 插件 → R4 gateway，零断点。

验证 ARCH-v5 §6 联通链：守卫降级信号 → settle 选级 → scope.target
→ gateway 切后端 / REFUSE 真拒答 → Result → R1 _settle。
"""
from __future__ import annotations

import asyncio

from hooks import Decision, Host, Plugin, RequestScope, Signal
from harness.model import ModelCapability
from harness.model.gateway import ModelGateway
from harness.model.registry import InMemoryModelRegistry
from harness.resilience import (
    CircuitBreaker, DegradeLadder, DegradeMode, DegradeStep, Degradation,
)
from hooks import LLMPort
from hooks.dto import LLMRequest, LLMResponse, Usage
from runtime import Effect, EffectKind, Result, Runner, Session


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


class _ForceBreaker(Plugin):
    name = "force_breaker"

    def __init__(self, ladder: DegradeLadder, available: set[str]) -> None:
        self.degradation = Degradation(ladder, available=available)

    def apply(self, host: Host) -> None:
        class Guard:
            name = "guard"

            def apply(self, h: Host) -> None:
                h.hook("before_invoke").tap(self.name, self.check)

            async def check(self, scope: RequestScope, effect) -> Decision:
                scope.emit(Signal("degrade", "breaker_open", "primary"))
                return Decision.degrade("")

        host.plugin(Guard()).plugin(self.degradation)


def _program():
    resp = yield Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]})
    yield Effect(EffectKind.END, {"output": resp.get("content", "?") if isinstance(resp, dict) else "?"})


class TestV5EndToEnd:
    def test_degrade_switches_to_backup_endpoint(self) -> None:
        """熔断降级 → scope.target=backup → gateway 切 backup，primary 零调用。"""
        primary = _FakeLLM("primary")
        backup = _FakeLLM("backup")
        cap = ModelCapability(llm=primary)
        cap.add_backend("backup", backup)
        gw = cap.gateway()

        host = Host()
        ladder = DegradeLadder(steps=(
            DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
            DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        host.plugin(_ForceBreaker(ladder, {"primary", "backup"})).freeze()

        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="v5-e2e", program_factory=_program)
        result = asyncio.run(runner.run(session))
        assert result.output == "backup", f"降级未切端点：{result.output}"
        assert backup.calls == 1 and primary.calls == 0

    def test_refuse_truly_refuses_no_backend_call(self) -> None:
        """available 全剔除 → REFUSE → gateway 返回 REFUSED，零后端调用。"""
        primary = _FakeLLM("primary")
        cap = ModelCapability(llm=primary)
        gw = cap.gateway()

        host = Host()
        ladder = DegradeLadder(steps=(
            DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        host.plugin(_ForceBreaker(ladder, set())).freeze()

        runner = Runner(dispatch=host.dispatch, call=host.call(gw.as_call()))  # type: ignore[arg-type]
        session = Session(session_id="v5-refuse", program_factory=_program)
        result = asyncio.run(runner.run(session))
        assert primary.calls == 0, "REFUSE 时不应调用任何后端"
        # 拒答在首步 Result（events[0].results[0]）与系统消息里，不在 RunResult.error
        first_results = result.events[0].results
        assert any(
            r.error is not None and r.error.code == "REFUSED" for r in first_results
        ), f"未返回 REFUSED 失败信封：{first_results}"
        assert any("[REFUSED]" in m.content for m in result.state.messages), \
            "拒答未进状态消息"
