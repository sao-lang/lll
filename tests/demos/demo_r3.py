"""R3 独立 demo：可靠性插件迁入 AOP 五角色 + 降级四坑（B1/B2/R3-6/R3-7）。

不依赖外部服务：时钟/随机源/存储显式注入，全程确定性可回放。
演示 CircuitBreaker 三态机、Degradation 按会话隔离选级（四坑语义）、
失败分类/退避/幂等等领域逻辑；结尾输出 PASS 标记。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aop import FaultRecord, Invocation, Verdict  # noqa: E402
from runtime import Invoke, Result  # noqa: E402

from harness.resilience import (  # noqa: E402
    BreakerStatus, CircuitBreaker, DegradeLadder, DegradeMode,
    Degradation, DegradeStep, Idempotency, RetryPolicy,
)


class _Session:
    def __init__(self, sid: str) -> None:
        self.session_id = sid


def _inv(model: str = "primary", *, idem: str | None = None) -> Invocation:
    return Invocation(
        effect=Invoke(port="llm", payload={"model": model}, idempotency_key=idem),
        ctx=type("Ctx", (), {"session_id": "demo", "meta": {}})(),
        session=_Session("demo"),
        meta={},
    )


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


async def main() -> None:
    # 1) Degradation 四坑：B2 注册名 / R3-6 永久剔除 / B1 会话隔离
    deg = Degradation(_ladder(), available={"primary", "backup"})
    assert deg.select(FaultRecord("primary_down", "primary"), session_id="s1") == "backup"
    assert deg.select(FaultRecord("none", ""), session_id="s1") == "backup"  # 写回不回退
    assert deg.select(FaultRecord("none", ""), session_id="s2") == "primary"  # 会话隔离
    assert deg.select(FaultRecord("backup_down", "backup"), session_id="s1") == "refuse"

    # 2) CircuitBreaker 三态机：closed → open（冷却内拦截）
    now = [0.0]
    cb = CircuitBreaker(minimum_samples=3, clock=lambda: now[0], open_cooldown_s=30.0)
    fail = Result.failure("500", "server error")
    for _ in range(3):
        await cb.record(fail, ("provider", "openai"))
    assert (await cb.state(("provider", "openai"))).status == BreakerStatus.OPEN.value
    assert await cb.check(("provider", "openai")) == "breaker_open"
    now[0] = 31.0
    assert await cb.check(("provider", "openai")) is None  # 冷却后半开放行

    # 3) 幂等去重（TTL 真淘汰）
    idem = Idempotency(ttl=60, clock=lambda: 1000.0)
    assert (await idem.guard(_inv(idem="K"))).allowed is True
    assert (await idem.guard(_inv(idem="K"))).allowed is False

    # 4) 重试退避（注入 sleeper，无真实等待）
    delays: list[float] = []
    async def sleep(d: float) -> None:
        delays.append(d)
    retry = RetryPolicy(max_attempts=3, base_delay=0.1, jitter="none", sleeper=sleep)
    async def always_fail(inv: Invocation) -> Result:
        return Result.failure("500", "boom")
    result = await retry.wrap(_inv(), always_fail)
    assert not result.ok and delays == [0.1, 0.2]
    print("PASS demo_r3: AOP 角色插件 + 降级四坑 + 熔断三态 + 幂等 + 退避")


if __name__ == "__main__":
    asyncio.run(main())
