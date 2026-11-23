"""R4 独立 demo：ModelCapability + 领域执行器降级切 backup + REFUSE 真拒答。

内联 fake LLMPort（不联网、确定性）跑三条链路：健康走 primary、真实限流策略写
fault 后 Degradation 选级切 backup、可用目标全剔除则 REFUSE 真拒答（零后端调用）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import LLMRequest, LLMResponse, Usage  # noqa: E402
from runtime import Invoke, Result, Runner, Session, Terminate  # noqa: E402

from harness.model import ModelCapability  # noqa: E402
from harness.resilience import (  # noqa: E402
    DegradeLadder, DegradeMode, Degradation, DegradeStep,
    RateLimit, ReliabilityExecutor, ResilienceCapability,
)


class FakeLLM:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def complete(self, req: LLMRequest, ctx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.name, usage=Usage(prompt_tokens=1))

    async def stream(self, req, ctx):
        yield self.name

    @property
    def last_usage(self) -> Usage:
        return Usage(prompt_tokens=1)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _limit() -> RateLimit:
    """令牌恒不足：真实限流策略返回 DEGRADE，由执行器写具名 fault。"""
    return RateLimit(rate=0.0, burst=0, on_exceed="degrade", clock=lambda: 0.0)


def _program():
    resp = yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Terminate(output=resp.get("content", "?") if isinstance(resp, dict) else "?")


async def _run(executor: ReliabilityExecutor, sid: str) -> object:
    return await Runner(call=executor.as_call()).run(Session(session_id=sid, program_factory=_program))


async def main() -> None:
    # 健康：primary
    primary = FakeLLM("primary")
    cap = ModelCapability(llm=primary)
    r = await _run(ResilienceCapability().wrap(cap.gateway().as_call()), "healthy")
    assert r.output == "primary" and primary.calls == 1

    # 降级：切 backup
    primary2, backup = FakeLLM("primary"), FakeLLM("backup")
    cap2 = ModelCapability(llm=primary2)
    cap2.add_backend("backup", backup)
    deg = Degradation(_ladder(), available={"primary", "backup"})
    c2 = ResilienceCapability(rate_limit=_limit(), degradation=deg).wrap(cap2.gateway().as_call())
    r2 = await _run(c2, "degrade")
    assert r2.output == "backup" and backup.calls == 1 and primary2.calls == 0

    # 拒答：无可用目标 → REFUSE 真拒答，零后端调用
    primary3 = FakeLLM("primary")
    cap3 = ModelCapability(llm=primary3)
    cap3.add_backend("backup", FakeLLM("backup"))
    deg3 = Degradation(_ladder(), available=set())
    c3 = ResilienceCapability(rate_limit=_limit(), degradation=deg3).wrap(cap3.gateway().as_call())
    r3 = await _run(c3, "refuse")
    assert primary3.calls == 0 and not r3.events[0].results[0].ok
    assert r3.events[0].results[0].error.code == "REFUSED"
    print("PASS demo_r4: 健康 primary / 降级 backup / REFUSE 真拒答")


if __name__ == "__main__":
    asyncio.run(main())
