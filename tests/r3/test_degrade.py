"""R3-T4 其余防线测试：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。"""
from __future__ import annotations

from hooks import Decision, DecisionAction

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
from runtime import Result
from tests.r3.conftest import make_ctx, ok_result


def _ladder() -> DegradeLadder:
    """测试降级阶梯：主→备→拒答。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


class TestDegradation:
    def test_all_unavailable_refuse(self) -> None:
        """所有目标不可用→返回拒答级（FALLBACK 带 quality=0 的拒答值）。"""
        d = Degradation(_ladder())
        ctx = make_ctx()
        choice = d.pick(set())
        assert choice.mode is DegradeMode.REFUSE

    def test_pick_best_available(self) -> None:
        """选可用的最高质量级。"""
        d = Degradation(_ladder())
        assert d.pick({"primary", "backup"}).target == "primary"
        assert d.pick({"backup"}).target == "backup"

    def test_cycle_rejected_on_init(self) -> None:
        """成环阶梯装配期拒绝。"""
        bad = DegradeLadder(steps=(
            DegradeStep(target="x", mode=DegradeMode.REROUTE, quality=1.0),
            DegradeStep(target="x", mode=DegradeMode.REROUTE, quality=0.5),
            DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
        ))
        try:
            Degradation(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for cyclic ladder")


class TestRateLimit:
    def test_token_bucket_exhaust(self) -> None:
        """令牌耗尽后拒绝（on_exceed=reject → DENY）。"""
        rl = RateLimit(rate=60, burst=2, on_exceed="reject", clock=lambda: 0.0)
        ctx = make_ctx()
        assert rl.check(ctx).action is DecisionAction.CONTINUE
        assert rl.check(ctx).action is DecisionAction.CONTINUE
        assert rl.check(ctx).action is DecisionAction.DENY

    def test_refill_over_time(self) -> None:
        """随时间补充令牌。"""
        now = [0.0]
        rl = RateLimit(rate=60, burst=1, on_exceed="reject", clock=lambda: now[0])
        ctx = make_ctx()
        assert rl.check(ctx).action is DecisionAction.CONTINUE
        assert rl.check(ctx).action is DecisionAction.DENY
        now[0] = 2.0  # 60/s → 2 秒补满
        assert rl.check(ctx).action is DecisionAction.CONTINUE


class TestBulkhead:
    def test_pool_limit(self) -> None:
        """舱壁池满：拒绝新占用。"""
        bh = Bulkhead(pools={"default": 1}, on_full="reject")
        ctx = make_ctx()
        assert bh.acquire("default", ctx) is True
        assert bh.acquire("default", ctx) is False
        bh.release("default")
        assert bh.acquire("default", ctx) is True


class TestIdempotency:
    def test_dedup(self) -> None:
        """同一幂等键第二次命中：返回去重标记。"""
        idem = Idempotency()
        assert idem.check_and_mark("k1") is False  # 首次未见过
        assert idem.check_and_mark("k1") is True   # 第二次命中


class _DictStore:
    """killswitch 用的最小 store fake（get 支持默认值，兼容 MutableMapping 语义）。"""

    def __init__(self) -> None:
        self.data: dict[str, bool] = {}

    def get(self, key: str, default: bool = False) -> bool:
        return self.data.get(key, default)

    def set(self, key: str, value: bool) -> None:
        self.data[key] = value


class TestWatchdog:
    def test_abab_detect(self) -> None:
        """A-B-A-B 交替 k 次→abort。"""
        wd = Watchdog(window=6, repeat_k=2, detect_abab=True)
        ctx = make_ctx()
        for kind in ("A", "B", "A", "B"):
            d = wd.observe(kind, ctx)
        assert d.action is DecisionAction.ABORT

    def test_repeat_detect(self) -> None:
        """同一 kind 连续重复 k 次→abort。"""
        wd = Watchdog(window=10, repeat_k=3, detect_abab=True)
        ctx = make_ctx()
        for _ in range(3):
            d = wd.observe("A", ctx)
        assert d.action is DecisionAction.ABORT


class TestKillSwitch:
    def test_flag_set_aborts(self) -> None:
        """标志置位→abort。"""
        store = _DictStore()
        store.set("kill:session", True)
        ks = KillSwitch(flag_store=store, scope="session")
        d = ks.check(make_ctx())
        assert d.action is DecisionAction.ABORT

    def test_flag_unset_continues(self) -> None:
        store = _DictStore()
        ks = KillSwitch(flag_store=store, scope="session")
        assert ks.check(make_ctx()).action is DecisionAction.CONTINUE


class TestIdempotencyTTL:
    def test_ttl_evicts_expired_key(self) -> None:
        """TTL 真淘汰：过期后同键视为首次（此前 ttl 只是不生效的元数据）。"""
        now = [1000.0]
        idem = Idempotency(ttl=60, clock=lambda: now[0])
        assert idem.check_and_mark("k") is False   # 首次
        assert idem.check_and_mark("k") is True    # 窗口内重复
        now[0] += 61                                # 推进过 TTL
        assert idem.check_and_mark("k") is False   # 已淘汰，重新视为首次

    def test_explicit_store_receives_timestamps(self) -> None:
        """注入 store 存的是登记时间戳（可被外部检查/共享）。"""
        store: dict[str, float] = {}
        idem = Idempotency(store=store, ttl=10, clock=lambda: 500.0)
        idem.check_and_mark("x")
        assert store["x"] == 500.0
