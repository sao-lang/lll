"""R3 其余防线：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch / 软预算。

三体系正交后，插件保留一等身份（name + 实例状态 + 构造注入），只把实现接口换成
AOP 角色：
- Warden：KillSwitch / Watchdog / Idempotency / RateLimit（guard → Verdict）。
- Envelope：Bulkhead（acquire + try/finally release）。
- Refiner：BudgetGuard（改 Invocation，不写 State）。
- Salvage：Degradation（唯一降级裁决者，保 B1/B2/R3-6/R3-7 四坑）。

时钟/存储显式注入，墙钟/随机不隐式进入，保证可测试、可回放。内核零策略。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, MutableMapping
from enum import Enum

from aop import FaultRecord, Invocation, RoutingTarget, Verdict
from runtime import Budgets, Invoke, Result

from .types import DegradeLadder, DegradeMode, DegradeStep, validate_ladder

__all__ = [
    "BudgetGuard",
    "Bulkhead",
    "Degradation",
    "Idempotency",
    "KillSwitch",
    "RateLimit",
    "Watchdog",
]


def _failed_target_of(inv: Invocation) -> str:
    """从 Invocation 派生失败目标注册名（B2：阶梯后端注册名，非维度二元组）。

    参数：
        inv: 调用上下文；effect 为 Invoke 时读 payload.model，缺省 "primary"。
    返回：
        后端注册名。
    """
    effect = inv.effect
    if isinstance(effect, Invoke):
        return str(effect.payload.get("model") or "primary")
    return "primary"


class _ExceedPolicy(str, Enum):
    """超限策略的有限取值。

    成员：
        WAIT: 降级（写 fault 交 Degradation 选级）。
        REJECT: 直接拒绝（Verdict(allowed=False)）。
        DEGRADE: 降级（同 WAIT）。
    """
    WAIT = "wait"
    REJECT = "reject"
    DEGRADE = "degrade"

    @classmethod
    def coerce(cls, raw: _ExceedPolicy | str) -> _ExceedPolicy:
        """字符串归一为成员；未知值显式回退 DEGRADE（fail-closed）。"""
        if isinstance(raw, cls):
            return raw
        try:
            return cls(raw)
        except ValueError:
            return cls.DEGRADE


class _KillScope(str, Enum):
    """killswitch 作用域的有限取值。"""
    SESSION = "session"
    GLOBAL = "global"

    @classmethod
    def coerce(cls, raw: _KillScope | str) -> _KillScope:
        """字符串归一为成员；未知值回退 SESSION（最小权限）。"""
        if isinstance(raw, cls):
            return raw
        try:
            return cls(raw)
        except ValueError:
            return cls.SESSION


class Degradation:
    """降级插件（Salvage）：唯一裁决者，持有 ladder + available + 按会话隔离状态。

    修复历史四坑：
        B1：目标状态按 session_id 隔离，禁容器级单值（跨 Effect 在同一会话继承）。
        B2：failed_target 必须是阶梯注册名（由上游 _failed_target_of 派生）。
        R3-6：故障目标永久从实例 available 剔除并写回（非局部副本）。
        R3-7：上游故障记录必须携带 failed_target，选级据此剔除。
    """
    name = "r3_degrade"

    def __init__(
        self,
        ladder: DegradeLadder,
        *,
        available: set[str] | None = None,
        session_state: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """装配降级插件：阶梯强校验；可用集缺省空（走 REFUSE）。

        参数：
            ladder: 降级阶梯（无环、末级 REFUSE、quality 严格递减）。
            available: 初始可用注册名集合（各会话的初始模板）；None 视为空集。
            session_state: 按 session_id 隔离的状态表（B1），每会话持自己的 available 副本。
        """
        validate_ladder(ladder)
        self._ladder = ladder
        self._initial_available = set(available) if available is not None else set()
        self._session_state = session_state if session_state is not None else {}

    def _session(self, session_id: str) -> dict[str, object]:
        """取本会话状态切片（B1，缺失则按初始 available 建独立副本）。

        每个会话拥有独立的 available 集合；一个会话剔除 primary 不影响另一会话，
        彻底消除旧架构"全局单槽串味"（B1）。
        """
        state = self._session_state.get(session_id)
        if state is None:
            state = {"available": set(self._initial_available)}
            self._session_state[session_id] = state
        return state

    def pick(self, available: set[str] | None = None) -> DegradeStep:
        """从给定（或初始）可用集里选 quality 最高级，全无则 REFUSE。"""
        return self._ladder.pick(available if available is not None else set(self._initial_available))

    def select(self, fault: FaultRecord, *, session_id: str) -> str:
        """唯一选级裁决（四坑落点）。

        分派：
            - B1：读写本会话独立状态（不碰全局单值）。
            - R3-6：把 fault.failed_target 永久从本会话 available 剔除并写回。
            - 沿 ladder 从高 quality 选本会话 available 中仍可用的目标；全剔除 → refuse。
            - B2：failed_target 已是注册名（由上游保证），此处只做幂等剔除。
        """
        state = self._session(session_id)  # B1：本会话独立切片（跨 Effect 继承锚点）
        available: set[str] = state["available"]  # type: ignore[assignment]
        if fault.failed_target:
            # R3-6：永久剔除并写回本会话集合（非局部副本 discard）。
            available.discard(fault.failed_target)
            state["available"] = available
        choice = self.pick(available)
        return choice.target or DegradeMode.REFUSE.value

    async def recover(self, exc: Exception, inv: Invocation) -> Result:
        """Salvage 入口：读 inv.meta 故障记录 → 选级 → 写 typed routing_target。

        参数：
            exc: 中心调用异常（可能 None 由容器补占位）。
            inv: 调用上下文（含 fault 记录与 session_id）。
        返回：
            带目标的成功 Result（目标写入 inv.meta["routing_target"] 供网关消费）。
        """
        fault = inv.fault()
        if fault is None:
            fault = FaultRecord(reason="invoke_failed", failed_target=_failed_target_of(inv))
        target = self.select(fault, session_id=inv.session_id)
        # typed 目标路径：与 Registry 同源的注册名写入具名记录，供 R4 网关消费。
        inv.write_routing_target(RoutingTarget(target=target, reason=fault.reason))
        if target == DegradeMode.REFUSE.value:
            # 末级拒答：真拒答失败 Result，绝不编造答案、绝不调后端。
            return Result.failure("REFUSED", "degrade ladder exhausted; refusal")
        return Result.success(target)


class RateLimit:
    """限流插件（Warden）：令牌桶，时钟显式注入。"""
    name = "r3_ratelimit"

    def __init__(
        self,
        *,
        rate: float = 60.0,
        burst: int = 10,
        on_exceed: _ExceedPolicy | str = _ExceedPolicy.WAIT,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """装配令牌桶：rate 令牌/秒，burst 容量，超限策略 on_exceed。"""
        self._rate = rate
        self._burst = burst
        self._on_exceed = _ExceedPolicy.coerce(on_exceed)
        self._clock = clock or (lambda: 0.0)
        self._tokens = float(burst)
        self._last = self._clock()

    def _refill(self) -> None:
        """按经过时间补充令牌（上限 burst）。"""
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
        self._last = now

    def check(self) -> Verdict | None:
        """取一个令牌；不足按策略返回拒绝或降级意图（None 表示降级类故障）。

        返回：
            Verdict(allowed=True) 放行；Verdict(allowed=False) 拒绝；
            None 表示超限降级（由调用方写 fault 并放行重路由）。
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return Verdict(allowed=True)
        if self._on_exceed is _ExceedPolicy.REJECT:
            return Verdict(allowed=False, reason="rate_limited")
        return None  # 降级类：调用方写 fault(reason="rate_limited") 后放行

    async def guard(self, inv: Invocation) -> Verdict:
        """Warden.guard：超限降级时写 fault（含 failed_target，B2）并放行。"""
        verdict = self.check()
        if verdict is None:
            inv.write_fault(FaultRecord(reason="rate_limited", failed_target=_failed_target_of(inv)))
            return Verdict(allowed=True)  # 降级类放行，容器据此重路由
        return verdict


class Bulkhead:
    """舱壁插件（Envelope）：按池隔离并发，池满拒绝；用 try/finally 保证释放。"""
    name = "r3_bulkhead"

    def __init__(
        self,
        *,
        pools: dict[str, int] | None = None,
        on_full: _ExceedPolicy | str = _ExceedPolicy.REJECT,
        pool: str = "default",
    ) -> None:
        """装配舱壁：pools 池名→容量；on_full 满时策略；pool 本插件池名。"""
        self._capacity = dict(pools or {"default": 10})
        self._used: dict[str, int] = {name: 0 for name in self._capacity}
        self._on_full = _ExceedPolicy.coerce(on_full)
        self._pool = pool

    def acquire(self, pool: str) -> bool:
        """占用一个槽；成功 True，池满 False。"""
        cap = self._capacity.get(pool, self._capacity.get("default", 0))
        used = self._used.get(pool, 0)
        if used >= cap:
            return False
        self._used[pool] = used + 1
        return True

    def release(self, pool: str) -> None:
        """释放一个槽（下限 0，防重复释放）。"""
        self._used[pool] = max(0, self._used.get(pool, 0) - 1)

    async def wrap(self, inv: Invocation, next: Callable[[Invocation], object]) -> Result:
        """Envelope.wrap：占槽 → 调 next → finally 释放（替掉旧 scope.releases）。"""
        if not self.acquire(self._pool):
            if self._on_full is _ExceedPolicy.REJECT:
                return Result.failure("BULKHEAD_FULL", "bulkhead_full")
            inv.write_fault(FaultRecord(reason="bulkhead_full", failed_target=_failed_target_of(inv)))
        try:
            outcome = next(inv)
            return await outcome if hasattr(outcome, "__await__") else outcome  # type: ignore[return-value]
        finally:
            self.release(self._pool)


class Idempotency:
    """幂等插件（Warden）：按幂等键去重，TTL 到期自动淘汰。"""
    name = "r3_idempotency"

    def __init__(
        self,
        *,
        store: MutableMapping[str, float] | None = None,
        ttl: float = 86400,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """装配去重存储（值为登记时间戳）与 TTL（真淘汰）。"""
        self._store = store if store is not None else {}
        self._ttl = ttl
        self._clock = clock or time.time

    def check_and_mark(self, key: str) -> bool:
        """返回 True 表示窗口内已见过（命中去重），False 表示首次（并登记）。"""
        now = self._clock()
        marked_at = self._store.get(key)
        if marked_at is not None and now - marked_at >= self._ttl:
            self._store.pop(key, None)
            marked_at = None
        if marked_at is not None:
            return True
        self._store[key] = now
        return False

    async def guard(self, inv: Invocation) -> Verdict:
        """Warden.guard：读 Invoke.idempotency_key，重复副作用拒绝。"""
        effect = inv.effect
        key = effect.idempotency_key if isinstance(effect, Invoke) else None
        if key and self.check_and_mark(key):
            return Verdict(allowed=False, reason="duplicate_side_effect")
        return Verdict(allowed=True)


class Watchdog:
    """看门狗（Warden）：检测重复 / A-B 交替循环，命中则拒绝。"""
    name = "r3_watchdog"

    def __init__(
        self,
        *,
        window: int = 10,
        repeat_k: int = 3,
        detect_abab: bool = True,
        kinds: Collection[str] | None = None,
    ) -> None:
        """装配循环检测：window 观察窗，repeat_k 重复阈值，detect_abab 是否查交替。"""
        self._window = window
        self._repeat_k = repeat_k
        self._detect_abab = detect_abab
        self._kinds = frozenset(kinds) if kinds else None
        self._history: list[str] = []

    def _kind_of(self, inv: Invocation) -> str | None:
        """由 Invoke.port 派生观测标识；不在 kinds 白名单内跳过。"""
        effect = inv.effect
        if not isinstance(effect, Invoke):
            return None
        kind = effect.port
        if self._kinds is not None and kind not in self._kinds:
            return None
        return kind

    def observe(self, kind: str) -> Verdict:
        """记录观测点；命中重复或交替模式返回拒绝。"""
        self._history.append(kind)
        self._history = self._history[-self._window:]
        if self._repeat_k > 0 and len(self._history) >= self._repeat_k:
            tail = self._history[-self._repeat_k:]
            if len(set(tail)) == 1:
                return Verdict(allowed=False, reason="watchdog:repeat")
        if self._detect_abab and len(self._history) >= 4:
            tail = self._history[-4:]
            if tail[0] == tail[2] and tail[1] == tail[3] and tail[0] != tail[1]:
                return Verdict(allowed=False, reason="watchdog:abab")
        return Verdict(allowed=True)

    async def guard(self, inv: Invocation) -> Verdict:
        """Warden.guard：观测标识由 Invoke.port 派生，命中循环即拒绝。"""
        kind = self._kind_of(inv)
        if kind is None:
            return Verdict(allowed=True)
        return self.observe(kind)


class KillSwitch:
    """紧急开关（Warden）：标志置位时拒绝（人工/运维一键熔断）。"""
    name = "r3_killswitch"

    def __init__(
        self,
        *,
        flag_store: MutableMapping[str, bool] | None = None,
        scope: _KillScope | str = _KillScope.SESSION,
    ) -> None:
        """装配标志存储与作用域（未知值回退 SESSION）。"""
        self.flag_store = flag_store if flag_store is not None else {}
        self._scope = _KillScope.coerce(scope)

    async def guard(self, inv: Invocation) -> Verdict:
        """Warden.guard：任一全局标志或本作用域标志置位 → 拒绝。"""
        keys = ("kill:global", f"kill:{self._scope.value}")
        if any(self.flag_store.get(k, False) for k in keys):
            return Verdict(allowed=False, reason="killswitch")
        return Verdict(allowed=True)


class BudgetGuard:
    """软预算守卫（Refiner）：达软阈值时给 Invocation.meta 注入收尾提示。

    与 R1 硬限分工：R1 Budgets.exceeded() 硬拒答；BudgetGuard 在硬限前给模型软提示。
    不写 State（回放/指纹前提），只产新 Invocation。
    """
    name = "r3_budgetguard"

    _HINT = "Budget is running low. Wrap up and give a final answer; do not start new tasks."

    def __init__(self, *, soft: float = 0.8, budgets: Budgets | None = None) -> None:
        """装配软阈值与预算投影。

        异常：
            ValueError: soft 不在 (0, 1]。
        """
        if not 0.0 < soft <= 1.0:
            raise ValueError("soft must be in (0, 1]")
        self._soft = soft
        self._budgets = budgets

    def check(self, budgets: Budgets | None = None) -> bool:
        """任一维度用量/上限 ≥ soft 且未到硬限时，返回 True。"""
        current = budgets if budgets is not None else self._budgets
        if current is None:
            return False
        return (
            (current.max_turns > 0 and current.used_turns / current.max_turns >= self._soft)
            or (current.max_hops > 0 and current.used_hops / current.max_hops >= self._soft)
            or (current.max_rounds > 0 and current.used_rounds / current.max_rounds >= self._soft)
            or (current.max_cost_usd > 0 and current.used_cost_usd / current.max_cost_usd >= self._soft)
            or (current.max_tokens > 0 and current.used_tokens / current.max_tokens >= self._soft)
            or (current.max_parallel_children > 0
                and current.used_parallel_children / current.max_parallel_children >= self._soft)
        )

    def refine_hint(self, value: str) -> str:
        """达软阈值时把收尾提示追加到文本（供测试直接验证领域逻辑）。"""
        if not self.check():
            return value
        return f"{value}\n{self._HINT}" if value else self._HINT

    async def refine(self, inv: Invocation) -> Invocation:
        """Refiner.refine：达软阈值时产新 Invocation（meta 注入提示，不写 State）。"""
        if not self.check():
            return inv
        ctx = inv.ctx
        # 规则 6 例外：RuntimeCtx 是装配期注入的鸭子上下文，按具名字段读取预算投影。
        budget = getattr(ctx, "state", None)
        budgets = getattr(budget, "budgets", None) if budget is not None else None
        if not self.check(budgets):
            return inv
        inv.meta["budget_hint"] = self._HINT
        return inv
