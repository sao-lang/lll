"""R3 其余防线测试（ARCH-v5）：降级选级 / 限流令牌桶 / 舱壁池 / 幂等 / watchdog / killswitch。

守卫统一返回 Decision 并可通过 RequestScope.emit(Signal) 报降级意图；
降级选级由 Degradation.select(signals) 直接裁决（available 收缩，末级 REFUSE）。
不再依赖 ControlState / ctx.meta / HookBus.emit / HookMode.DEGRADER。
"""
from __future__ import annotations

from hooks import DecisionAction, RequestScope, Signal
from runtime import Effect, EffectKind

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

EFFECT = Effect(EffectKind.LLM, {})
TOOL_EFFECT = Effect(EffectKind.TOOL, {})


def _ladder() -> DegradeLadder:
    """测试降级阶梯：主→备→拒答。"""
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


class TestDegradationSelection:
    def test_pick_best_available(self) -> None:
        """从可用目标集选 quality 最高的一级；无可用落 REFUSE。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.pick({"primary", "backup"}).target == "primary"
        assert plugin.pick({"backup"}).target == "backup"
        refuse = plugin.pick(set())
        assert refuse.mode is DegradeMode.REFUSE and refuse.target == ""

    def test_select_shrinks_available_step_by_step(self) -> None:
        """按失败信号剔除 failed_target：逐级选下一级，全剔除落 REFUSE。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        # 无信号：选最优目标
        assert plugin.select([]) == "primary"
        # primary 失败 → 剔除 primary → 选 backup
        one_fail = [Signal("degrade", "primary_down", "primary")]
        assert plugin.select(one_fail) == "backup"
        # primary + backup 都失败 → 全部剔除 → REFUSE
        two_fail = one_fail + [Signal("degrade", "backup_down", "backup")]
        assert plugin.select(two_fail) == DegradeMode.REFUSE.value

    def test_select_ignores_non_degrade_and_unknown_failed_target(self) -> None:
        """非 degrade 信号 / 不在 available 的 failed_target 不影响选级。"""
        plugin = Degradation(_ladder(), available={"primary", "backup"})
        assert plugin.select([Signal("abort", "manual")]) == "primary"
        assert plugin.select([Signal("degrade", "ghost_down", "ghost")]) == "primary"

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
        """令牌耗尽后拒绝（on_exceed=reject → DENY，无降级信号）。"""
        rl = RateLimit(rate=60, burst=2, on_exceed="reject", clock=lambda: 0.0)
        scope = RequestScope()
        assert rl.check(scope, EFFECT).action is DecisionAction.CONTINUE
        assert rl.check(scope, EFFECT).action is DecisionAction.CONTINUE
        assert rl.check(scope, EFFECT).action is DecisionAction.DENY
        assert scope.signals == []

    def test_refill_over_time(self) -> None:
        """随时间补充令牌。"""
        now = [0.0]
        rl = RateLimit(rate=60, burst=1, on_exceed="reject", clock=lambda: now[0])
        scope = RequestScope()
        assert rl.check(scope, EFFECT).action is DecisionAction.CONTINUE
        assert rl.check(scope, EFFECT).action is DecisionAction.DENY
        now[0] = 2.0  # 60/s → 2 秒补满
        assert rl.check(scope, EFFECT).action is DecisionAction.CONTINUE

    def test_degrade_reports_intent(self) -> None:
        """超限降级：scope 报 degrade 信号 + 占位 DEGRADE（target 由 settle 选）。"""
        rl = RateLimit(rate=60, burst=1, on_exceed="degrade", clock=lambda: 0.0)
        scope = RequestScope()
        assert rl.check(scope, EFFECT).action is DecisionAction.CONTINUE  # 耗尽令牌
        d = rl.check(scope, EFFECT)  # 超限 → DEGRADE
        assert d.action is DecisionAction.DEGRADE
        assert d.target == ""  # 占位，真实目标由 settle 覆盖
        assert any(
            s.kind == "degrade" and s.reason == "rate_limited" for s in scope.signals
        )

    def test_reject_does_not_request_degrade(self) -> None:
        """reject 策略：DENY，不产生降级信号。"""
        rl = RateLimit(rate=60, burst=1, on_exceed="reject", clock=lambda: 0.0)
        scope = RequestScope()
        rl.check(scope, EFFECT)
        assert rl.check(scope, EFFECT).action is DecisionAction.DENY
        assert scope.signals == []


class TestBulkhead:
    def test_pool_limit(self) -> None:
        """舱壁池满：check 拒绝新占用，一轮结束统一释放后可再占。"""
        bh = Bulkhead(pools={"default": 1}, pool="default", on_full="reject")
        scope = RequestScope()
        assert bh.check(scope, EFFECT).action is DecisionAction.CONTINUE  # 占槽
        assert len(scope.releases) == 1, "check 应登记释放回调"
        assert bh.check(scope, EFFECT).action is DecisionAction.DENY      # 池满
        # 生产由 Host 在每轮结束统一 release_all（成功/失败/守卫短路都兜底），
        # 测试走同一路径，不手调 release。
        scope.release_all()
        assert scope.releases == []
        assert bh.check(scope, EFFECT).action is DecisionAction.CONTINUE

    def test_degrade_reports_intent(self) -> None:
        """池满降级：scope 报 degrade 信号 + 占位 DEGRADE。"""
        bh = Bulkhead(pools={"default": 1}, on_full="degrade")
        scope = RequestScope()
        assert bh.check(scope, EFFECT).action is DecisionAction.CONTINUE  # 占满
        d = bh.check(scope, EFFECT)  # 池满 → DEGRADE
        assert d.action is DecisionAction.DEGRADE
        assert d.target == ""
        assert any(
            s.kind == "degrade" and s.reason == "bulkhead_full" for s in scope.signals
        )

    def test_reject_does_not_request_degrade(self) -> None:
        """reject 策略：DENY，不产生降级信号。"""
        bh = Bulkhead(pools={"default": 1}, on_full="reject")
        bh.check(RequestScope(), EFFECT)
        scope = RequestScope()
        assert bh.check(scope, EFFECT).action is DecisionAction.DENY
        assert scope.signals == []


class TestIdempotency:
    def test_dedup(self) -> None:
        """同一幂等键第二次命中：返回去重标记。"""
        idem = Idempotency()
        assert idem.check_and_mark("k1") is False  # 首次未见过
        assert idem.check_and_mark("k1") is True   # 第二次命中

    def test_check_reads_effect_idempotency_key(self) -> None:
        """before_invoke 回调从 effect.idempotency_key 读键，重复副作用 DENY。"""
        idem = Idempotency()
        eff = Effect(EffectKind.TOOL, {}, idempotency_key="k1")
        assert idem.check(RequestScope(), eff).action is DecisionAction.CONTINUE
        d = idem.check(RequestScope(), eff)
        assert d.action is DecisionAction.DENY


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
        """A-B-A-B 交替 → abort（观测 kind 由直送 effect 派生，不手填）。"""
        wd = Watchdog(window=6, repeat_k=2, detect_abab=True, kinds={"tool", "llm"})
        verdict = None
        for eff in (TOOL_EFFECT, EFFECT, TOOL_EFFECT, EFFECT):
            verdict = wd.check(RequestScope(), eff)
        assert verdict is not None and verdict.action is DecisionAction.ABORT

    def test_repeat_detect(self) -> None:
        """同一 kind 连续重复 k 次 → abort。"""
        wd = Watchdog(window=10, repeat_k=3, detect_abab=True, kinds={"tool"})
        verdict = None
        for _ in range(3):
            verdict = wd.check(RequestScope(), TOOL_EFFECT)
        assert verdict is not None and verdict.action is DecisionAction.ABORT


class TestKillSwitch:
    def test_flag_set_aborts(self) -> None:
        """标志置位 → abort。"""
        store = _DictStore()
        store.set("kill:session", True)
        ks = KillSwitch(flag_store=store, scope="session")
        assert ks.check(RequestScope(), EFFECT).action is DecisionAction.ABORT

    def test_flag_unset_continues(self) -> None:
        store = _DictStore()
        ks = KillSwitch(flag_store=store, scope="session")
        assert ks.check(RequestScope(), EFFECT).action is DecisionAction.CONTINUE


class TestIdempotencyTTL:
    def test_ttl_evicts_expired_key(self) -> None:
        """TTL 真淘汰：过期后同键视为首次。"""
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
