"""R3 其余防线：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。

全部是 Hook 插件或辅助策略，返回 Decision；内核零策略（R3 §1）。
时钟/存储显式注入，墙钟/随机不隐式进入，保证可测试、可回放。
"""
from __future__ import annotations

from collections.abc import Callable, MutableMapping

from hooks import Decision, HookCtx, HookMode

from .types import DegradeLadder, DegradeMode, DegradeStep, validate_ladder

__all__ = [
    "Degradation", "RateLimit", "Bulkhead", "Idempotency", "Watchdog", "KillSwitch",
]


class Degradation:
    """降级插件：按 DegradeLadder 选可用目标，装配期强校验（无环/末级拒答）。"""

    name = "r3_degrade"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 50
    requires_ports: tuple[str, ...] = ()

    def __init__(self, ladder: DegradeLadder, *, available: set[str] | None = None) -> None:
        """装配降级阶梯；成环/无拒答兜底在装配期拒绝。

        参数：
            ladder: 降级阶梯。available: 当前可用目标集合（缺省空→拒答）。
        异常：
            ValueError: 阶梯成环 / 末级非拒答 / quality 未递减。
        """
        validate_ladder(ladder)
        self._ladder = ladder
        self._available = available if available is not None else set()

    def pick(self, available: set[str] | None = None) -> DegradeStep:
        """从可用目标选最高质量级（缺省用构造时的 available）。"""
        return self._ladder.pick(available if available is not None else self._available)

    async def __call__(self, ctx: HookCtx) -> Decision:
        """GUARD：若有可用降级目标且被显式请求（meta['degrade']），返回 DEGRADE。"""
        if ctx.meta.get("force_degrade"):
            choice = self.pick()
            return Decision.degrade(choice.target or DegradeMode.REFUSE.value)
        return Decision.continue_()


class RateLimit:
    """限流插件：令牌桶（token bucket），时钟显式注入。"""

    name = "r3_ratelimit"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 30
    requires_ports: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        rate: float = 60.0,
        burst: int = 10,
        on_exceed: str = "wait",
        clock: Callable[[], float] | None = None,
    ) -> None:
        """装配令牌桶：rate 令牌/秒，burst 容量，超限策略 on_exceed。"""
        self._rate = rate
        self._burst = burst
        self._on_exceed = on_exceed
        self._clock = clock or (lambda: 0.0)
        self._tokens = float(burst)
        self._last = self._clock()

    def _refill(self) -> None:
        """按经过时间补充令牌（上限 burst）。"""
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
        self._last = now

    def check(self, ctx: HookCtx) -> Decision:
        """取一个令牌；不足按策略返回 DENY（reject）或 DEGRADE（wait/degrade）。"""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return Decision.continue_()
        ctx.trace.append("ratelimit:exceed")
        if self._on_exceed == "reject":
            return Decision.deny("rate_limited")
        return Decision.degrade("rate_limited")

    async def __call__(self, ctx: HookCtx) -> Decision:
        return self.check(ctx)


class Bulkhead:
    """舱壁插件：按池隔离并发，池满拒绝（防故障扩散）。"""

    name = "r3_bulkhead"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 20
    requires_ports: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        pools: dict[str, int] | None = None,
        acquire_timeout: float = 5.0,
        on_full: str = "reject",
    ) -> None:
        """装配舱壁：pools 为 池名→容量；on_full 为满时策略。"""
        self._capacity = dict(pools or {"default": 10})
        self._used: dict[str, int] = {name: 0 for name in self._capacity}
        self._timeout = acquire_timeout
        self._on_full = on_full

    def acquire(self, pool: str, ctx: HookCtx | None = None) -> bool:
        """占用一个槽内；成功 True，池满 False。"""
        cap = self._capacity.get(pool, self._capacity.get("default", 0))
        used = self._used.get(pool, 0)
        if used >= cap:
            return False
        self._used[pool] = used + 1
        return True

    def release(self, pool: str) -> None:
        """释放一个槽（下限 0，防重复释放）。"""
        self._used[pool] = max(0, self._used.get(pool, 0) - 1)

    async def __call__(self, ctx: HookCtx) -> Decision:
        pool = str(ctx.meta.get("bulkhead_pool", "default"))
        if self.acquire(pool, ctx):
            return Decision.continue_()
        return Decision.deny("bulkhead_full") if self._on_full == "reject" else Decision.degrade("bulkhead_full")


class Idempotency:
    """幂等插件：按幂等键去重，防止重复执行副作用。"""

    name = "r3_idempotency"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 10
    requires_ports: tuple[str, ...] = ()

    def __init__(self, *, store: MutableMapping[str, bool] | None = None, ttl: int = 86400) -> None:
        """装配去重存储（缺省进程内字典）与 TTL（仅元数据，单实例闭环不淘汰）。"""
        self._store = store if store is not None else {}
        self._ttl = ttl

    def check_and_mark(self, key: str) -> bool:
        """返回 True 表示已见过（命中去重），False 表示首次（并登记）。"""
        if key in self._store:
            return True
        self._store[key] = True
        return False

    async def __call__(self, ctx: HookCtx) -> Decision:
        key = ctx.effect.payload.get("x-idempotency-key")
        if key and self.check_and_mark(str(key)):
            ctx.trace.append("idempotency:duplicate")
            return Decision.deny("duplicate_side_effect")
        return Decision.continue_()


class Watchdog:
    """看门狗：检测重复 / A-B 交替循环，命中则 abort（防死循环）。"""

    name = "r3_watchdog"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 0
    requires_ports: tuple[str, ...] = ()

    def __init__(self, *, window: int = 10, repeat_k: int = 3, detect_abab: bool = True) -> None:
        """装配循环检测：window 观察窗，repeat_k 重复阈值，detect_abab 是否查交替。"""
        self._window = window
        self._repeat_k = repeat_k
        self._detect_abab = detect_abab
        self._history: list[str] = []

    def observe(self, kind: str, ctx: HookCtx | None = None) -> Decision:
        """记录一个观测点；命中重复或交替模式返回 ABORT。"""
        self._history.append(kind)
        self._history = self._history[-self._window:]
        if self._repeat_k > 0 and len(self._history) >= self._repeat_k:
            tail = self._history[-self._repeat_k:]
            if len(set(tail)) == 1:
                return Decision.abort("watchdog:repeat")
        if self._detect_abab and len(self._history) >= 4:
            tail = self._history[-4:]
            if tail[0] == tail[2] and tail[1] == tail[3] and tail[0] != tail[1]:
                return Decision.abort("watchdog:abab")
        return Decision.continue_()

    async def __call__(self, ctx: HookCtx) -> Decision:
        kind = str(ctx.meta.get("watchdog_kind", ctx.effect.kind.value))
        return self.observe(kind, ctx)


class KillSwitch:
    """紧急开关：标志置位时 abort（人工/运维一键熔断全局）。"""

    name = "r3_killswitch"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = -10
    requires_ports: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        flag_store: MutableMapping[str, bool] | None = None,
        scope: str = "session",
    ) -> None:
        """装配标志存储（缺省进程内字典）与作用域（session/global）。"""
        self.flag_store = flag_store if flag_store is not None else {}
        self._scope = scope

    def check(self, ctx: HookCtx) -> Decision:
        """任一全局标志或会话标志置位 → ABORT。"""
        keys = [f"kill:global", f"kill:{self._scope}"]
        if any(self.flag_store.get(k, False) for k in keys):
            ctx.trace.append("killswitch:triggered")
            return Decision.abort("killswitch")
        return Decision.continue_()

    async def __call__(self, ctx: HookCtx) -> Decision:
        return self.check(ctx)
