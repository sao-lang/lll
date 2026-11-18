"""R4 独立 demo：ModelCapability + add_backend + gateway + REFUSE 真拒答。

用内联 fake LLMPort（不联网、确定性）跑三条链路：健康走 primary、守卫降级切
backup、可用目标全剔除则 REFUSE 真拒答（零后端调用，绝不回退默认模型）。
降级由 host + guard 插件 emit 信号触发，settle 选级后写入 scope.target 供网关读取。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hooks import Decision, Host, LLMRequest, LLMResponse, Signal, Usage  # noqa: E402

from harness.model import ModelCapability  # noqa: E402
from harness.resilience import (  # noqa: E402
    DegradeLadder, DegradeMode, DegradeStep, Degradation,
)
from runtime import Effect, EffectKind, Runner, Session  # noqa: E402


class _FakeLLM:
    """内联最小 LLMPort fake：只记录调用次数，返回自身名字作为响应。"""

    name: str

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def complete(self, request: LLMRequest, ctx: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=self.name,
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    async def stream(self, request: LLMRequest, ctx: object):  # type: ignore[no-untyped-def]
        yield self.name

    @property
    def last_usage(self) -> Usage:
        return Usage(prompt_tokens=1, completion_tokens=1)


class _Guard:
    """降级守卫插件：before_invoke 上 emit 故障信号并返回占位 DEGRADE。"""

    name = "r4_guard"

    def apply(self, host: Host) -> None:
        host.hook("before_invoke").tap(self.name, self.check)

    async def check(self, scope: object, effect: object) -> Decision:
        scope.emit(Signal("degrade", "breaker_open", "primary"))  # type: ignore[attr-defined]
        return Decision.degrade("")


def _ladder() -> DegradeLadder:
    """降级阶梯：primary → backup → 拒答。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():  # type: ignore[no-untyped-def]
    """最小 R1 程序：一次 LLM Effect + 一个 END。"""
    resp = yield Effect(EffectKind.LLM, {
        "messages": [{"role": "user", "content": "hi"}],
    })
    yield Effect(EffectKind.END, {
        "output": resp.get("content", "?") if isinstance(resp, dict) else "?",
    })


async def _run(host: Host, gateway: object, name: str) -> object:
    """把 host 与 gateway 接到 Runner 上跑一遍 _program。"""
    runner = Runner(
        dispatch=host.dispatch,
        call=host.call(gateway.as_call()),  # type: ignore[attr-defined,arg-type]
    )
    return await runner.run(Session(session_id=name, program_factory=_program))


async def main() -> None:
    # 1) 健康链路：capability 构造 gateway，默认后端 primary 被调用
    primary = _FakeLLM("primary")
    cap = ModelCapability(llm=primary)
    healthy = Host().freeze()
    healthy_result = await _run(healthy, cap.gateway(), "r4-healthy")
    assert healthy_result.output == "primary"  # type: ignore[union-attr]
    assert primary.calls == 1

    # 2) 降级链路：add_backend 注册 backup，guard 触发降级 → gateway 切 backup
    primary2 = _FakeLLM("primary")
    backup2 = _FakeLLM("backup")
    cap2 = ModelCapability(llm=primary2)
    cap2.add_backend("backup", backup2)
    degraded = Host()
    degraded.plugin(_Guard()).plugin(
        Degradation(_ladder(), available={"primary", "backup"})
    ).freeze()
    degraded_result = await _run(degraded, cap2.gateway(), "r4-degrade")
    assert degraded_result.output == "backup"  # type: ignore[union-attr]
    assert backup2.calls == 1 and primary2.calls == 0

    # 3) REFUSE 链路：available 全剔除 → target=refuse → 真拒答，零后端调用
    primary3 = _FakeLLM("primary")
    backup3 = _FakeLLM("backup")
    cap3 = ModelCapability(llm=primary3)
    cap3.add_backend("backup", backup3)
    refused = Host()
    refused.plugin(_Guard()).plugin(
        Degradation(_ladder(), available=set())
    ).freeze()
    refused_result = await _run(refused, cap3.gateway(), "r4-refuse")
    assert primary3.calls == 0 and backup3.calls == 0
    first_results = refused_result.events[0].results  # type: ignore[union-attr]
    assert any(r.error is not None and r.error.code == "REFUSED" for r in first_results)
    assert any("[REFUSED]" in m.content for m in refused_result.state.messages)  # type: ignore[union-attr]

    print("[R4] PASS: capability+add_backend / gateway 切后端 / guard 降级信号链 / REFUSE 真拒答")


asyncio.run(main())
