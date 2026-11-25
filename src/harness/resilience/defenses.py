"""R3 其余防线：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。

每个插件只暴露**自身的领域语义方法**（限流许可 acquire、舱位占放 acquire/release、
幂等认领 claim、循环探测 inspect、开关查询 is_tripped、降级选级 select），不再有
统一拦截协议。编排由 ReliabilityExecutor 显式调用这些方法。

时钟/存储显式注入，墙钟/随机不隐式进入，保证可测试、可回放。内核零策略。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, MutableMapping
from enum import Enum

from compose.context import FaultRecord, Invocation
from runtime import Invoke, Result

from .types import DegradeLadder, DegradeMode, DegradeStep, validate_ladder

__all__ = [
    "Bulkhead",
    "Degradation",
    "Idempotency",
    "KillSwitch",
    "RatePermit",
    "RateLimit",
    "Watchdog",
    "failed_target_of",
]


def failed_target_of(inv: Invocation) -> str:
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


type NextFn = Callable[[Invocation], Awaitable[Result]]
"""领域动作续延（舱壁 run_in_slot 包裹的下一跳）。"""


class RatePermit(str, Enum):
    """限流许可裁决（限流领域语义）。

    成员：
        ADMIT: 取到令牌，放行。
        REJECT: 拒绝（fail-fast）。
        DEGRADE: 超限降级，交由执行器写 fault 并选级。
    """
    ADMIT = "admit"
    REJECT = "reject"
    DEGRADE = "degrade"


class _ExceedPolicy(str, Enum):
    """超限策略的有限取值。

    成员：
        WAIT: 降级（写 fault 交 Degradation 选级）。
        REJECT: 直接拒绝（fail-fast）。
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
    """降级策略：唯一选级裁决者，持有 ladder + available + 按会话隔离状态。

    修复历史四坑：
        B1：目标状态按 session_id 隔离，禁容器级单值（跨 Effect 在同一会话继承）。
        B2：failed_target 必须是阶梯注册名（由上游 failed_target_of 派生）。
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

class RateLimit:
    """限流策略：令牌桶许可，时钟显式注入。"""
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

    def acquire(self) -> RatePermit:
        """取一个令牌（限流领域语义）。

        返回：
            RatePermit.ADMIT 放行；RatePermit.REJECT 拒绝；
            RatePermit.DEGRADE 超限降级（执行器写 fault 并选级）。
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return RatePermit.ADMIT
        if self._on_exceed is _ExceedPolicy.REJECT:
            return RatePermit.REJECT
        return RatePermit.DEGRADE


class Bulkhead:
    """舱壁策略：按池隔离并发，池满拒绝；用 try/finally 保证释放。"""
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

    async def run_in_slot(self, inv: Invocation, next: NextFn) -> Result:
        """舱位隔离执行（领域语义）：占槽 → 调下一跳 → finally 释放。

        只有真正 acquire 成功的调用才释放槽位。满池 DEGRADE 路径虽继续执行
        next，但它没占用槽，finally 不得释放——否则会释放持槽者的槽击穿隔离。
        """
        acquired = self.acquire(self._pool)
        if not acquired:
            if self._on_full is _ExceedPolicy.REJECT:
                return Result.failure("BULKHEAD_FULL", "bulkhead_full")
            inv.write_fault(FaultRecord(reason="bulkhead_full", failed_target=failed_target_of(inv)))
        if not acquired:
            # DEGRADE：降级执行但不占槽，不进入释放分支。
            return await next(inv)
        try:
            return await next(inv)
        finally:
            self.release(self._pool)


class Idempotency:
    """幂等策略：按幂等键去重认领，TTL 到期自动淘汰。"""
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

class Watchdog:
    """看门狗：检测重复 / A-B 交替循环，命中则拒绝。"""
    name = "r3_watchdog"

    def __init__(
        self,
        *,
        window: int = 10,
        repeat_k: int = 3,
        detect_abab: bool = True,
        kinds: Collection[str] | None = None,
        max_sessions: int = 1024,
    ) -> None:
        """装配循环检测：window 观察窗，repeat_k 重复阈值，detect_abab 是否查交替。

        max_sessions: 会话历史桶上限（LRU 淘汰最久未活动桶），防多租户长生命周期
        策略实例无界泄漏；0 表示不限制（兼容需要无限历史的场景）。
        """
        self._window = window
        self._repeat_k = repeat_k
        self._detect_abab = detect_abab
        self._kinds = frozenset(kinds) if kinds else None
        self._max_sessions = max_sessions
        # 历史按会话分桶：跨会话交替调用不得形成假循环（B1 会话隔离）。
        # OrderedDict 供 LRU 淘汰：活跃会话访问即移到末尾，淘汰最久未用桶。
        self._history: OrderedDict[str, list[str]] = OrderedDict()

    def _kind_of(self, inv: Invocation) -> str | None:
        """由 Invoke.port 派生观测标识；不在 kinds 白名单内跳过。"""
        effect = inv.effect
        if not isinstance(effect, Invoke):
            return None
        kind = effect.port
        if self._kinds is not None and kind not in self._kinds:
            return None
        return kind

    def inspect(self, kind: str, session_id: str = "") -> bool:
        """记录一个观测点并检测循环（看门狗领域语义）。

        返回 True 表示命中重复或 A-B 交替模式（执行器据此终止拒绝），False 正常。
        session_id 区分会话历史：缺省空串供单会话直接调用（兼容既有领域 API）。
        """
        history = self._history.get(session_id)
        if history is None:
            history = []
            self._history[session_id] = history
        else:
            self._history.move_to_end(session_id)  # LRU：活跃会话移到末尾
        history.append(kind)
        del history[:-self._window]
        # 桶上限淘汰：最久未活动的会话先走，活跃桶刚 move_to_end 不受影响。
        if self._max_sessions > 0:
            while len(self._history) > self._max_sessions:
                self._history.popitem(last=False)
        if self._repeat_k > 0 and len(history) >= self._repeat_k:
            tail = history[-self._repeat_k:]
            if len(set(tail)) == 1:
                return True
        if self._detect_abab and len(history) >= 4:
            tail = history[-4:]
            if tail[0] == tail[2] and tail[1] == tail[3] and tail[0] != tail[1]:
                return True
        return False

    def detect(self, inv: Invocation) -> bool:
        """从 Invoke.port 派生观测标识并检测循环（执行器入口）。

        非关注类型（不在 kinds 白名单 / 非 Invoke）返回 False（不拦截）。
        """
        kind = self._kind_of(inv)
        if kind is None:
            return False
        session = getattr(inv, "session", None)
        sid = str(getattr(session, "session_id", "") or "")
        return self.inspect(kind, session_id=sid)


class KillSwitch:
    """紧急开关：标志置位时拒绝（人工/运维一键熔断）。"""
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

    def is_tripped(self, session_id: str) -> bool:
        """紧急开关是否置位（领域语义）：全局标志或本作用域标志为真即置位。"""
        keys = ("kill:global", f"kill:{self._scope.value}")
        return any(self.flag_store.get(k, False) for k in keys)
