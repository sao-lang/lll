"""RED：降级 reroute 前必须重跑 preflight（killswitch 翻转后不得再调 backup）。

裂缝：ReliabilityExecutor._invoke 只在最开头跑一次 _preflight，reroute 循环
直接调 _budgeted_call。若首轮 core 失败并在失败处理中翻转 killswitch（运维
紧急熔断），reroute 仍会调用 backup → 绕过紧急开关。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from runtime import Result
from harness.resilience import DegradeLadder, DegradeMode, DegradeStep, Degradation, KillSwitch
from harness.resilience.executor import ReliabilityExecutor
from tests.r3.conftest import make_ctx


def test_red_killswitch_flip_blocks_reroute() -> None:
    flags: dict[str, bool] = {}
    ks = KillSwitch(flag_store=flags, scope="session")
    ladder = DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))
    degradation = Degradation(ladder, available={"primary", "backup"})
    calls: list[str] = []

    def core(inv):
        target = inv.routing_target()
        name = target.target if target else "primary"
        calls.append(name)
        if name == "primary":
            # 首轮失败，并在此时翻转紧急开关
            inv.write_fault(__import__("compose.context", fromlist=["FaultRecord"]).FaultRecord(
                reason="primary_down", failed_target="primary"))
            flags["kill:session"] = True
            return Result.failure("PRIMARY_DOWN", "down")
        return Result.success(name)

    executor = ReliabilityExecutor(core, kill_switch=ks, degradation=degradation)
    result = asyncio.run(executor.run_invocation(make_ctx()))
    assert "backup" not in calls, f"killswitch 翻转后不得 reroute 到 backup，实际调用 {calls}"
    assert not result.ok and result.error and "killswitch" in str(result.error.message), \
        f"应以 killswitch 拒绝，实际 {result}"
