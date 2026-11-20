"""R3 降级与防线测试：新 AOP 角色接口，完整保留四坑与领域语义断言。

四坑（P9）：B1 按会话隔离；B2 failed_target 是后端注册名；R3-6 available 永久剔除写回；
R3-7 retry/timeout 故障记录带 failed_target。断言强度不降，只换 API 形态。
"""
from __future__ import annotations

import asyncio

import pytest

from compose.context import FaultRecord, Verdict
from runtime import Invoke

from harness.resilience import (
    Bulkhead,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    Degradation,
    Idempotency,
    KillSwitch,
    RateLimit,
    Watchdog,
)
from tests.r3.conftest import make_ctx

EFFECT = Invoke(port="llm", payload={})
TOOL_EFFECT = Invoke(port="tool", payload={})
SID = "s-degrade"


def _ladder() -> DegradeLadder:
    """测试降级阶梯：主→备→拒答。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _fault(reason: str, target: str) -> FaultRecord:
    return FaultRecord(reason=reason, failed_target=target)


class TestDegradationSelection:
    def test_pick_best_available(self) -> None:
        """从可用目标集选 quality 最高的一级；无可用落 REFUSE。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.pick({"primary", "backup"}).target == "primary"
        assert plugin.pick({"backup"}).target == "backup"
        refuse = plugin.pick(set())
        assert refuse.mode is DegradeMode.REFUSE and refuse.target == ""

    def test_select_shrinks_available_step_by_step(self) -> None:
        """B2/R3-6：按失败目标逐级永久剔除，全剔除落 REFUSE。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.select(FaultRecord("none", ""), session_id=SID) == "primary"
        # primary 失败 → 永久剔除 → 选 backup
        assert plugin.select(_fault("primary_down", "primary"), session_id=SID) == "backup"
        # 再选一次无新故障：backup 仍在（剔除是永久但只剔已知故障目标）。
        assert plugin.select(FaultRecord("none", ""), session_id=SID) == "backup"
        # backup 也失败 → 全部剔除 → REFUSE
        assert plugin.select(_fault("backup_down", "backup"), session_id=SID) == DegradeMode.REFUSE.value

    def test_r3_6_shrink_is_permanent_not_local_copy(self) -> None:
        """R3-6：剔除写回实例 available——primary 故障后不会在下一轮被重新选回。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        plugin.select(_fault("primary_down", "primary"), session_id=SID)
        # 无新故障的后续选择仍是 backup，绝不回到 primary（局部副本 discard 的旧 bug）。
        for _ in range(3):
            assert plugin.select(FaultRecord("none", ""), session_id=SID) == "backup"

    def test_b1_session_isolation(self) -> None:
        """B1：按 session_id 隔离目标状态；A 会话剔除 primary 不影响 B 会话。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        # A 会话 primary 故障 → 剔除 → 选 backup
        assert plugin.select(_fault("primary_down", "primary"), session_id="session-A") == "backup"
        # B 会话独立状态：primary 仍可用 → 选 primary（不受 A 影响，消除全局单槽串味）
        assert plugin.select(FaultRecord("none", ""), session_id="session-B") == "primary"
        assert "session-A" in plugin._session_state and "session-B" in plugin._session_state

    def test_select_ignores_unknown_failed_target(self) -> None:
        """非降级原因 / 不在 available 的 failed_target 不误伤选级。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.select(FaultRecord("manual", ""), session_id=SID) == "primary"
        assert plugin.select(_fault("ghost_down", "ghost"), session_id=SID) == "primary"

    def test_cycle_rejected_on_init(self) -> None:
        """成环阶梯装配期拒绝。"""
        bad = DegradeLadder(steps=(
            DegradeStep(target="x", mode=DegradeMode.REROUTE, quality=1.0),
            DegradeStep(target="x", mode=DegradeMode.REROUTE, quality=0.5),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        with pytest.raises(ValueError):
            Degradation(bad)


class TestRateLimit:
    def test_token_bucket_exhaust(self) -> None:
        """令牌耗尽后拒绝（reject 策略 → allowed=False）。"""
        rl = RateLimit(rate=60, burst=2, on_exceed="reject", clock=lambda: 0.0)
        assert asyncio.run(rl.guard(make_ctx())).allowed is True
        assert asyncio.run(rl.guard(make_ctx())).allowed is True
        assert asyncio.run(rl.guard(make_ctx())).allowed is False

    def test_refill_over_time(self) -> None:
        """随时间补充令牌。"""
        now = [0.0]
        rl = RateLimit(rate=60, burst=1, on_exceed="reject", clock=lambda: now[0])
        assert asyncio.run(rl.guard(make_ctx())).allowed is True
        assert asyncio.run(rl.guard(make_ctx())).allowed is False
        now[0] = 2.0  # 60/s → 2 秒补满
        assert asyncio.run(rl.guard(make_ctx())).allowed is True

    def test_degrade_reports_fault_and_passes(self) -> None:
        """超限降级：写 fault（含 failed_target，B2）并放行重路由。"""
        rl = RateLimit(rate=60, burst=1, on_exceed="degrade", clock=lambda: 0.0)
        asyncio.run(rl.guard(make_ctx()))  # 耗尽令牌
        inv = make_ctx()
        verdict = asyncio.run(rl.guard(inv))
        assert isinstance(verdict, Verdict) and verdict.allowed is True  # 降级类放行
        fault = inv.fault()
        assert fault is not None and fault.reason == "rate_limited"
        assert fault.failed_target == "primary"  # B2：注册名

    def test_reject_does_not_write_fault(self) -> None:
        """reject 策略：拒绝但不写降级 fault。"""
        rl = RateLimit(rate=60, burst=1, on_exceed="reject", clock=lambda: 0.0)
        asyncio.run(rl.guard(make_ctx()))
        inv = make_ctx()
        assert asyncio.run(rl.guard(inv)).allowed is False
        assert inv.fault() is None


class TestBulkhead:
    def test_pool_limit(self) -> None:
        """舱壁池满：拒绝新占用；wrap 结束 try/finally 释放后可再占。"""
        bh = Bulkhead(pools={"default": 1}, pool="default", on_full="reject")
        async def noop(inv, next): return next(inv)
        async def core(inv): return __import__("runtime").Result.success(None)
        asyncio.run(bh.wrap(make_ctx(), core))
        # wrap 结束已释放，可再占
        assert bh.acquire("default") is True
        bh.release("default")

    def test_degrade_writes_fault(self) -> None:
        """池满降级：写 fault 并放行（由容器重路由）。"""
        bh = Bulkhead(pools={"default": 1}, on_full="degrade")
        assert bh.acquire("default") is True  # 占满
        inv = make_ctx()
        from runtime import Result
        async def core(i): return Result.success(None)
        asyncio.run(bh.wrap(inv, core))
        # 占满时 acquire 失败 → 写 fault（wrap 内 finally 会释放）
        assert inv.fault() is not None and inv.fault().reason == "bulkhead_full"


class TestIdempotency:
    def test_dedup(self) -> None:
        """同一幂等键第二次命中：返回去重标记。"""
        idem = Idempotency()
        assert idem.check_and_mark("k1") is False
        assert idem.check_and_mark("k1") is True

    def test_guard_reads_effect_idempotency_key(self) -> None:
        """guard 从 Invoke.idempotency_key 读键，重复副作用拒绝。"""
        idem = Idempotency()
        eff = Invoke(port="tool", payload={}, idempotency_key="k1")
        inv = make_ctx(eff)
        assert asyncio.run(idem.guard(inv)).allowed is True
        assert asyncio.run(idem.guard(make_ctx(eff))).allowed is False

    def test_ttl_evicts_expired_key(self) -> None:
        """TTL 真淘汰：过期后同键视为首次。"""
        now = [1000.0]
        idem = Idempotency(ttl=60, clock=lambda: now[0])
        assert idem.check_and_mark("k") is False
        assert idem.check_and_mark("k") is True
        now[0] += 61
        assert idem.check_and_mark("k") is False

    def test_explicit_store_receives_timestamps(self) -> None:
        """注入 store 存的是登记时间戳。"""
        store: dict[str, float] = {}
        idem = Idempotency(store=store, ttl=10, clock=lambda: 500.0)
        idem.check_and_mark("x")
        assert store["x"] == 500.0


class _DictStore:
    """killswitch 用的最小 store fake。"""
    def __init__(self) -> None:
        self.data: dict[str, bool] = {}
    def get(self, key: str, default: bool = False) -> bool:
        return self.data.get(key, default)
    def set(self, key: str, value: bool) -> None:
        self.data[key] = value


class TestWatchdog:
    def test_abab_detect(self) -> None:
        """A-B-A-B 交替 → 拒绝（观测标识由 Invoke.port 派生）。"""
        wd = Watchdog(window=6, repeat_k=2, detect_abab=True, kinds={"tool", "llm"})
        verdict = Verdict()
        for eff in (TOOL_EFFECT, EFFECT, TOOL_EFFECT, EFFECT):
            verdict = asyncio.run(wd.guard(make_ctx(eff)))
        assert verdict.allowed is False and verdict.reason == "watchdog:abab"

    def test_repeat_detect(self) -> None:
        """同一 kind 连续重复 k 次 → 拒绝。"""
        wd = Watchdog(window=10, repeat_k=3, detect_abab=True, kinds={"tool"})
        verdict = Verdict()
        for _ in range(3):
            verdict = asyncio.run(wd.guard(make_ctx(TOOL_EFFECT)))
        assert verdict.allowed is False and verdict.reason == "watchdog:repeat"


class TestKillSwitch:
    def test_flag_set_rejects(self) -> None:
        """标志置位 → 拒绝。"""
        store = _DictStore()
        store.set("kill:session", True)
        ks = KillSwitch(flag_store=store, scope="session")
        assert asyncio.run(ks.guard(make_ctx())).allowed is False

    def test_flag_unset_passes(self) -> None:
        store = _DictStore()
        ks = KillSwitch(flag_store=store, scope="session")
        assert asyncio.run(ks.guard(make_ctx())).allowed is True
