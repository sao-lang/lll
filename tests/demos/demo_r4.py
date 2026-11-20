"""R4 独立 demo：ModelCapability + AOP 降级切 backup + REFUSE 真拒答。

内联 fake LLMPort（不联网、确定性）跑三条链路：健康走 primary、故障 Warden 写
fault 后 Degradation 选级切 backup、可用目标全剔除则 REFUSE 真拒答（零后端调用）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aop import AOPContainer, FaultRecord, Invocation, Verdict, Warden  # noqa: E402
from hooks import LLMRequest, LLMResponse, Usage  # noqa: E402
from runtime import Result, Runner, Session, Invoke, Terminate  # noqa: E402

from harness.model import ModelCapability  # noqa: E402
from harness.resilience import (  # noqa: E402
    DegradeLadder, DegradeMode, Degradation, DegradeStep,
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


class FaultWarden(Warden):
    name = "demo_fault"
    def __init__(self, reason: str) -> None:
        self.reason = reason
    async def guard(self, inv: Invocation) -> Verdict:
        inv.write_fault(FaultRecord(self.reason, "primary"))
        return Verdict(allowed=True)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _program():
    resp = yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Terminate(output=resp.get("content", "?") if isinstance(resp, dict) else "?")


async def _run(c: AOPContainer, sid: str) -> object:
    return await Runner(call=c.as_call()).run(Session(session_id=sid, program_factory=_program))


async def main() -> None:
    # 健康：primary
    primary = FakeLLM("primary")
    cap = ModelCapability(llm=primary)
    r = await _run(AOPContainer(cap.gateway().as_call()), "healthy")
    assert r.output == "primary" and primary.calls == 1

    # 降级：切 backup
    primary2, backup = FakeLLM("primary"), FakeLLM("backup")
    cap2 = ModelCapability(llm=primary2)
    cap2.add_backend("backup", backup)
    deg = Degradation(_ladder(), available={"primary", "backup"})
    c2 = AOPContainer(cap2.gateway().as_call()).add_warden(FaultWarden("rate_limited")).add_salvage(deg)
    r2 = await _run(c2, "degrade")
    assert r2.output == "backup" and backup.calls == 1 and primary2.calls == 0

    # 拒答：无可用目标 → REFUSE 真拒答，零后端调用
    primary3 = FakeLLM("primary")
    cap3 = ModelCapability(llm=primary3)
    cap3.add_backend("backup", FakeLLM("backup"))
    deg3 = Degradation(_ladder(), available=set())
    c3 = AOPContainer(cap3.gateway().as_call()).add_warden(FaultWarden("exhausted")).add_salvage(deg3)
    r3 = await _run(c3, "refuse")
    assert primary3.calls == 0 and not r3.events[0].results[0].ok
    assert r3.events[0].results[0].error.code == "REFUSED"
    print("PASS demo_r4: 健康 primary / 降级 backup / REFUSE 真拒答")


if __name__ == "__main__":
    asyncio.run(main())
