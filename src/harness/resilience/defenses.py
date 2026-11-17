"""R3 其余防线：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。

全部是 Hook 插件或辅助策略，返回 Decision；内核零策略（R3 §1）。
时钟/存储显式注入，墙钟/随机不隐式进入，保证可测试、可回放。
"""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from enum import Enum
import time

from harness._ctxview import coerce_ctx
from hooks import Decision, HookCtx, HookMode

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


class _ExceedPolicy(str, Enum):
    """超限策略的有限取值（规则 5；私有枚举，不进 G5 公共契约）。

    成员：
        WAIT: 等待/降级（令牌桶缺省，交上层走降级链）。
        REJECT: 直接拒绝（舱壁缺省）。
        DEGRADE: 降级。
    """

    WAIT = "wait"
    REJECT = "reject"
    DEGRADE = "degrade"

    @classmethod
    def coerce(cls, raw: _ExceedPolicy | str) -> _ExceedPolicy:
        """字符串归一为成员；未知值显式回退 DEGRADE（规则 5），不静默放行。"""
        if isinstance(raw, cls):
            return raw
        try:
            return cls(raw)
        except ValueError:
            return cls.DEGRADE


class _KillScope(str, Enum):
    """killswitch 作用域的有限取值（规则 5；私有枚举）。

    成员：SESSION 仅会话级标志；GLOBAL 含全局标志。
    """

    SESSION = "session"
    GLOBAL = "global"

    @classmethod
    def coerce(cls, raw: _KillScope | str) -> _KillScope:
        """字符串归一为成员；未知值显式回退 SESSION（最小权限，规则 5）。"""
        if isinstance(raw, cls):
            return raw
        try:
            return cls(raw)
        except ValueError:
            return cls.SESSION


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
        """GUARD：被显式请求降级时返回 DEGRADE，否则放行。

        参数：
            ctx: 钩子上下文；force_degrade 由 coerce_ctx 具名视图提供。
        返回：
            DEGRADE（选中的降级目标）或 CONTINUE。
        """
        if coerce_ctx(ctx).force_degrade:
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
        on_exceed: _ExceedPolicy | str = _ExceedPolicy.WAIT,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """装配令牌桶：rate 令牌/秒，burst 容量，超限策略 on_exceed。

        参数：
            rate: 每秒补充令牌数。burst: 桶容量。
            on_exceed: 超限策略，接受 _ExceedPolicy 或字符串，未知值回退 DEGRADE。
            clock: 注入时钟（秒），缺省常量 0（单实例闭环）。
        """
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

    def check(self, ctx: HookCtx) -> Decision:
        """取一个令牌；不足按策略返回 DENY（reject）或 DEGRADE（wait/degrade）。

        参数：
            ctx: 钩子上下文（仅写 trace）。
        返回：
            CONTINUE / DENY / DEGRADE。
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return Decision.continue_()
        ctx.trace.append("ratelimit:exceed")
        if self._on_exceed is _ExceedPolicy.REJECT:
            return Decision.deny("rate_limited")
        return Decision.degrade("rate_limited")

    async def __call__(self, ctx: HookCtx) -> Decision:
        """GUARD 入口：复用 check 的令牌桶判定。"""
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
        on_full: _ExceedPolicy | str = _ExceedPolicy.REJECT,
    ) -> None:
        """装配舱壁：pools 为池名→容量；on_full 为满时策略（未知值回退 DEGRADE）。

        参数：
            pools: 池名→并发上限；缺省 {"default": 10}。
            on_full: 池满策略，接受 _ExceedPolicy 或字符串。
        注：acquire_timeout 已移除（v2.3）——舱壁满即按 on_full 拒绝，无等待语义；
        保留一个不生效的超时参数会误导用户，故彻底删除。
        """
        self._capacity = dict(pools or {"default": 10})
        self._used: dict[str, int] = {name: 0 for name in self._capacity}
        self._on_full = _ExceedPolicy.coerce(on_full)

    def acquire(self, pool: str, ctx: HookCtx | None = None) -> bool:
        """占用一个槽内；成功 True，池满 False。

        参数：
            pool: 池名（未声明时回退 default 容量）。
            ctx: 保留参数（供未来 trace），当前不读取。
        """
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
        """GUARD 入口：按具名视图的池名占槽，满时按策略拒绝或降级。"""
        pool = coerce_ctx(ctx).bulkhead_pool
        if self.acquire(pool, ctx):
            return Decision.continue_()
        return (
            Decision.deny("bulkhead_full")
            if self._on_full is _ExceedPolicy.REJECT
            else Decision.degrade("bulkhead_full")
        )


class Idempotency:
    """幂等插件：按幂等键去重，TTL 到期自动淘汰（v2.3 补齐真淘汰）。"""

    name = "r3_idempotency"
    events = ("before_invoke",)
    mode = HookMode.GUARD
    priority = 10
    requires_ports: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        store: MutableMapping[str, float] | None = None,
        ttl: float = 86400,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """装配去重存储（值为登记时间戳）与 TTL（真淘汰）。

        参数：
            store: 幂等键→登记时间戳的映射（缺省进程内字典）。
            ttl: 去重窗口秒数；到期后同键视为首次，允许重试。
            clock: 注入时钟（缺省 time.time），便于测试确定性推进。
        """
        self._store = store if store is not None else {}
        self._ttl = ttl
        self._clock = clock or time.time  # 墙钟在此具名边界进入，不隐式散落

    def check_and_mark(self, key: str) -> bool:
        """返回 True 表示窗口内已见过（命中去重），False 表示首次（并登记）。

        先清理该键的过期记录（now - 登记时间 ≥ ttl 即淘汰），再判定。
        """
        now = self._clock()
        marked_at = self._store.get(key)
        if marked_at is not None and now - marked_at >= self._ttl:
            self._store.pop(key, None)
            marked_at = None
        if marked_at is not None:
            return True
        self._store[key] = now
        return False

    async def __call__(self, ctx: HookCtx) -> Decision:
        """GUARD 入口：幂等键来自具名视图，重复副作用直接 DENY。"""
        key = coerce_ctx(ctx).idempotency_key
        if key and self.check_and_mark(key):
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
        """记录一个观测点；命中重复或交替模式返回 ABORT。

        参数：
            kind: 观测标识（重复判定用）。ctx: 上下文（调用方传入，当前不读取）。
        返回：
            ABORT 或 CONTINUE。
        """
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
        """GUARD 入口：观测标识取自具名视图，缺省回退到 effect.kind。"""
        view = coerce_ctx(ctx)
        kind = view.watchdog_kind or ctx.effect.kind.value
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
        scope: _KillScope | str = _KillScope.SESSION,
    ) -> None:
        """装配标志存储（缺省进程内字典）与作用域（未知值回退 SESSION，规则 5）。

        参数：
            flag_store: 标志存储（可注入共享 store）。scope: 作用域枚举或字符串。
        """
        self.flag_store = flag_store if flag_store is not None else {}
        self._scope = _KillScope.coerce(scope)

    def check(self, ctx: HookCtx) -> Decision:
        """任一全局标志或本作用域标志置位 → ABORT。

        参数：
            ctx: 钩子上下文（仅写 trace）。
        返回：
            ABORT 或 CONTINUE。
        """
        keys = ("kill:global", f"kill:{self._scope.value}")
        if any(self.flag_store.get(k, False) for k in keys):
            ctx.trace.append("killswitch:triggered")
            return Decision.abort("killswitch")
        return Decision.continue_()

    async def __call__(self, ctx: HookCtx) -> Decision:
        """GUARD 入口：复用 check 的标志判定。"""
        return self.check(ctx)


class BudgetGuard:
    """软预算守卫：任一预算维度达到软阈值时注入"收尾"上下文（rewrite）。

    与 R1 硬限的分工（R3 §4）：R1 `Budgets.exceeded()` 负责硬上限拒答；
    BudgetGuard 在硬限之前给模型一个软提示，让它主动收尾而非撞墙。
    软阈值用 rewrite（context_inject）而非 abort——这是渐进降级，不是拒绝。
    """

    name = "r3_budgetguard"
    events = ("before_model",)
    mode = HookMode.TRANSFORM  # rewrite 需 transform mode 才会被 bus 应用
    priority = 90  # 晚于多数 guard/transform，最后再注入收尾提示
    requires_ports: tuple[str, ...] = ()

    def __init__(self, *, soft: float = 0.8) -> None:
        """装配软阈值。

        参数：
            soft: 触发收尾提示的用量比例（0~1），缺省 0.8。
        异常：
            ValueError: soft 不在 (0, 1]。
        """
        if not 0.0 < soft <= 1.0:
            raise ValueError("soft must be in (0, 1]")
        self._soft = soft

    def check(self, ctx: HookCtx) -> Decision:
        """任一维度用量/上限 ≥ soft 且未到硬限时，rewrite 注入收尾提示。"""
        budgets = ctx.budget_left
        if budgets is None:
            return Decision.continue_()  # 未注入预算投影：无信息，不误判
        # 显式六维判断：直接读具名字段（无 getattr 动态读取，满足规则 6；类型安全满足 mypy strict）
        hit = (
            (budgets.max_turns > 0 and budgets.used_turns / budgets.max_turns >= self._soft)
            or (budgets.max_hops > 0 and budgets.used_hops / budgets.max_hops >= self._soft)
            or (budgets.max_rounds > 0 and budgets.used_rounds / budgets.max_rounds >= self._soft)
            or (budgets.max_cost_usd > 0 and budgets.used_cost_usd / budgets.max_cost_usd >= self._soft)
            or (budgets.max_tokens > 0 and budgets.used_tokens / budgets.max_tokens >= self._soft)
            or (budgets.max_parallel_children > 0
                and budgets.used_parallel_children / budgets.max_parallel_children >= self._soft)
        )
        if hit:
            ctx.trace.append("budgetguard:soft")
            return Decision.rewrite(
                context="预算即将用尽，请收敛并给出最终答案，不要开启新任务。"
            )
        return Decision.continue_()

    async def __call__(self, ctx: HookCtx) -> Decision:
        """TRANSFORM 入口：复用 check 的软阈值判定。"""
        return self.check(ctx)
