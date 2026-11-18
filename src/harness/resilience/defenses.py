"""R3 其余防线：降级 / 限流 / 舱壁 / 幂等 / watchdog / killswitch。

v5（ARCH-v5）：插件只实现 apply(host)，在宿主定义的扩展点上 tap 回调；
守卫返回 Decision 短路，降级意图用 scope.emit(Signal("degrade", ...)) 表达，
由 settle 阶段统一选级；BudgetGuard 这类 WATERFALL 回调则直接改写上下文文本。
内核零策略（R3 §1）。时钟/存储显式注入，墙钟/随机不隐式进入，保证可测试、可回放。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, MutableMapping
from enum import Enum

from hooks import (
    Decision,
    Host,
    RequestScope,
    Signal,
    SignalKind,
)
from runtime import Budgets, Effect

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
    """降级插件：自身持有 ladder 与可用目标集，apply(host) 订阅 settle（BAIL）。

    修复旧架构"available 永不收缩"的空心化：每次 select 先从信号里的
    failed_target 剔除目标，再 pick 剩余最高质量级；全剔除则落 REFUSE。
    状态在插件实例上（注册一次、长期存活），跨调用共享。
    """

    name = "r3_degrade"

    def __init__(self, ladder: DegradeLadder, *, available: set[str] | None = None) -> None:
        """装配降级插件：阶梯装配期强校验，可用目标集缺省为空（走 REFUSE）。

        参数：
            ladder: 降级阶梯（无环、末级 REFUSE、quality 严格递减）。
            available: 初始可用注册名集合；None 视为空集。
        """
        validate_ladder(ladder)
        self._ladder = ladder
        self._available = set(available) if available is not None else set()

    def pick(self, available: set[str] | None = None) -> DegradeStep:
        """从给定（或自身）可用集里选 quality 最高级，全无则 REFUSE。"""
        return self._ladder.pick(available if available is not None else self._available)

    def select(self, signals: list[Signal]) -> str:
        """按降级信号选最终目标（跨 Effect 累积收缩，末级 REFUSE）。

        R3-6 修复：故障目标必须从插件实例的 available 里**永久剔除并写回**，而非只在
        局部副本 discard。v5.2 每次 Effect 新建 scope、signals 不继承，若剔除不写回，
        backup 故障时会重新从初始集合选回 primary，永远到不了 REFUSE。ladder 的 REFUSE
        级是无条件候选，目标剔除干净后 pick 自然落 REFUSE。
        """
        for sig in signals:
            if sig.kind == SignalKind.DEGRADE and sig.failed_target:
                # 只剔除当前可用集里的目标：已剔除的、未注册的都不误伤（幂等）。
                self._available.discard(sig.failed_target)
        choice = self.pick(self._available)
        return choice.target or DegradeMode.REFUSE.value

    def apply(self, host: Host) -> None:
        """挂载 settle 扩展点（BAIL）：有降级信号时选级。"""
        host.hook("settle").tap(self.name, self.settle)

    async def settle(self, scope: RequestScope, effect: Effect) -> Decision | None:
        """settle 回调：有降级意图才按累积收缩后的 available 选目标。"""
        if not any(s.kind == SignalKind.DEGRADE for s in scope.signals):
            return None
        return Decision.degrade(self.select(scope.signals))


class RateLimit:
    """限流插件：令牌桶（token bucket），时钟显式注入；订阅 before_invoke（BAIL）拦截。"""

    name = "r3_ratelimit"

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
            超限降级只 emit Signal("degrade", ...) 报告意图，target 由 settle 统一选。
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

    def check(self, scope: RequestScope, effect: Effect) -> Decision:
        """取一个令牌；不足按策略返回 DENY 或 DEGRADE（并 emit 降级信号）。

        令牌桶算法不变：time 推进补充、消耗一个令牌、不足按 on_exceed 处理。
        effect 由 Hook 直送，当前限流判定不读取（装配期参数已私有持有）。
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return Decision.continue_()
        scope.trace.append("ratelimit:exceed")
        if self._on_exceed is _ExceedPolicy.REJECT:
            return Decision.deny("rate_limited")  # deny 的 reason 是人读原因，保留状态串
        scope.emit(Signal(SignalKind.DEGRADE, "rate_limited"))
        return Decision.degrade("")  # 占位，settle 覆盖

    def apply(self, host: Host) -> None:
        """before_invoke（BAIL）：令牌不足即短路。"""
        host.hook("before_invoke").tap(self.name, self.check)


class Bulkhead:
    """舱壁插件：按池隔离并发，池满拒绝；订阅 before_invoke（BAIL）拦截。"""

    name = "r3_bulkhead"

    def __init__(
        self,
        *,
        pools: dict[str, int] | None = None,
        on_full: _ExceedPolicy | str = _ExceedPolicy.REJECT,
        pool: str = "default",
    ) -> None:
        """装配舱壁：pools 为池名→容量；on_full 为满时策略（未知值回退 DEGRADE）。

        参数：
            pools: 池名→并发上限；缺省 {"default": 10}。
            on_full: 池满策略，接受 _ExceedPolicy 或字符串。
            pool: 本插件占用的池名（装配期注入），缺省 "default"。
            池满降级只 emit Signal("degrade", ...) 报告意图，target 由 settle 统一选。
        注：acquire_timeout 已移除（v2.3）——舱壁满即按 on_full 拒绝，无等待语义；
        保留一个不生效的超时参数会误导用户，故彻底删除。
        """
        self._capacity = dict(pools or {"default": 10})
        self._used: dict[str, int] = {name: 0 for name in self._capacity}
        self._on_full = _ExceedPolicy.coerce(on_full)
        self._pool = pool

    def acquire(self, pool: str, scope: RequestScope | None = None) -> bool:
        """占用一个槽内；成功 True，池满 False。

        参数：
            pool: 池名（未声明时回退 default 容量）。
            scope: 保留参数（供未来 trace），当前不读取。
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

    def check(self, scope: RequestScope, effect: Effect) -> Decision:
        """before_invoke（BAIL）：按构造期池名占槽，满时拒绝或 emit 降级。"""
        if self.acquire(self._pool, scope):
            # 注册释放回调到 scope：无论后续守卫短路、Port 成功还是失败，
            # Host 都在本轮结束时统一释放（E2 完整修复，无泄漏边界）。
            scope.releases.append(lambda: self.release(self._pool))
            return Decision.continue_()
        if self._on_full is _ExceedPolicy.REJECT:
            return Decision.deny("bulkhead_full")  # deny reason 是人读状态串，保留
        scope.emit(Signal(SignalKind.DEGRADE, "bulkhead_full"))
        return Decision.degrade("")  # 占位，settle 覆盖

    def apply(self, host: Host) -> None:
        """before_invoke 占槽；释放由 scope.releases 统一兜底（Host 每轮结束调用）。"""
        host.hook("before_invoke").tap(self.name, self.check)


class Idempotency:
    """幂等插件：按幂等键去重，TTL 到期自动淘汰；订阅 before_invoke（BAIL）拦截。"""

    name = "r3_idempotency"

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

    def check(self, scope: RequestScope, effect: Effect) -> Decision:
        """before_invoke（BAIL）：幂等键读 effect.idempotency_key，重复副作用直接 DENY。"""
        key = effect.idempotency_key
        if key and self.check_and_mark(key):
            scope.trace.append("idempotency:duplicate")
            return Decision.deny("duplicate_side_effect")
        return Decision.continue_()

    def apply(self, host: Host) -> None:
        """before_invoke（BAIL）：重复副作用即短路。"""
        host.hook("before_invoke").tap(self.name, self.check)


class Watchdog:
    """看门狗：检测重复 / A-B 交替循环，命中则 abort；订阅 before_invoke（BAIL）。

    订阅选择：保留 before_invoke 而非 after_invoke。after_invoke 是 PARALLEL
    （tap 返回值被丢弃），且当前 host 的 settle 只消费 degrade 信号、不消费 abort，
    观察型订阅无法真正终止死循环；BAIL 下 Decision.abort 才会短路。
    """

    name = "r3_watchdog"

    def __init__(
        self,
        *,
        window: int = 10,
        repeat_k: int = 3,
        detect_abab: bool = True,
        kinds: Collection[str] | None = None,
    ) -> None:
        """装配循环检测：window 观察窗，repeat_k 重复阈值，detect_abab 是否查交替。

        kinds: 只观察这些 Effect kind 值（与 Matcher.kinds 同义）；None/空表示全观察。
        观测标识取 str(effect.kind.value)，由直送的 effect 派生。
        """
        self._window = window
        self._repeat_k = repeat_k
        self._detect_abab = detect_abab
        self._kinds = frozenset(kinds) if kinds else None
        self._history: list[str] = []

    def _kind_of(self, effect: Effect) -> str | None:
        """由 effect 派生观测标识；不在 kinds 白名单内则跳过观察。"""
        kind = str(effect.kind.value)
        if self._kinds is not None and kind not in self._kinds:
            return None
        return kind

    def observe(self, kind: str, scope: RequestScope | None = None) -> Decision:
        """记录一个观测点；命中重复或交替模式返回 ABORT。

        参数：
            kind: 观测标识（重复判定用）。scope: 请求容器（保留参数，当前不读取）。
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

    def check(self, scope: RequestScope, effect: Effect) -> Decision:
        """before_invoke（BAIL）：观测标识由 effect 派生，命中循环即短路 abort。"""
        kind = self._kind_of(effect)
        if kind is None:
            return Decision.continue_()
        return self.observe(kind, scope)

    def apply(self, host: Host) -> None:
        """before_invoke（BAIL）：一次调用观察一次，命中循环即短路 abort。"""
        host.hook("before_invoke").tap(self.name, self.check)


class KillSwitch:
    """紧急开关：标志置位时 abort（人工/运维一键熔断）；订阅 before_invoke（BAIL）。"""

    name = "r3_killswitch"

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

    def check(self, scope: RequestScope, effect: Effect) -> Decision:
        """before_invoke（BAIL）：任一全局标志或本作用域标志置位 → ABORT。"""
        keys = ("kill:global", f"kill:{self._scope.value}")
        if any(self.flag_store.get(k, False) for k in keys):
            scope.trace.append("killswitch:triggered")
            return Decision.abort("killswitch")
        return Decision.continue_()

    def apply(self, host: Host) -> None:
        """before_invoke（BAIL）：标志置位即短路。"""
        host.hook("before_invoke").tap(self.name, self.check)


class BudgetGuard:
    """软预算守卫：任一预算维度达软阈值时在上下文文本尾部注入"收尾"提示。

    与 R1 硬限的分工（R3 §4）：R1 `Budgets.exceeded()` 负责硬上限拒答；
    BudgetGuard 在硬限之前给模型一个软提示，让它主动收尾而非撞墙。
    订阅 transform（WATERFALL）：tap 回调签名是 `(value, effect) -> value`——接收上下文文本
    与 effect 直送参数，返回改写后的文本，不是 `(scope) -> Decision`。因此预算投影在装配时
    经 budgets 注入，达阈值时把收尾提示追加到 value（软阈值是渐进降级，不是拒绝）。
    """

    name = "r3_budgetguard"

    _HINT = "预算即将用尽，请收敛并给出最终答案，不要开启新任务。"

    def __init__(self, *, soft: float = 0.8, budgets: Budgets | None = None) -> None:
        """装配软阈值与预算投影。

        参数：
            soft: 触发收尾提示的用量比例（0~1），缺省 0.8。
            budgets: 预算投影来源；None 表示未注入，不误触发。
                     WATERFALL 回调不携带预算投影，故需在装配时给定。
        异常：
            ValueError: soft 不在 (0, 1]。
        """
        if not 0.0 < soft <= 1.0:
            raise ValueError("soft must be in (0, 1]")
        self._soft = soft
        self._budgets = budgets

    def check(self, budgets: Budgets | None = None) -> bool:
        """任一维度用量/上限 ≥ soft 且未到硬限时，返回 True（应注入收尾提示）。

        六维判断（turns/hops/rounds/cost/tokens/parallel_children）逻辑不变。
        """
        current = budgets if budgets is not None else self._budgets
        if current is None:
            return False  # 未注入预算投影：无信息，不误判
        # 显式六维判断：直接读具名字段（无 getattr 动态读取，满足规则 6；类型安全满足 mypy strict）
        return (
            (current.max_turns > 0 and current.used_turns / current.max_turns >= self._soft)
            or (current.max_hops > 0 and current.used_hops / current.max_hops >= self._soft)
            or (current.max_rounds > 0 and current.used_rounds / current.max_rounds >= self._soft)
            or (current.max_cost_usd > 0 and current.used_cost_usd / current.max_cost_usd >= self._soft)
            or (current.max_tokens > 0 and current.used_tokens / current.max_tokens >= self._soft)
            or (current.max_parallel_children > 0
                and current.used_parallel_children / current.max_parallel_children >= self._soft)
        )

    def rewrite(self, value: str, effect: Effect) -> str:
        """transform（WATERFALL）回调：`(value, effect) -> value`；达软阈值时追加收尾提示。"""
        if not self.check():
            return value
        return f"{value}\n{self._HINT}" if value else self._HINT

    def apply(self, host: Host) -> None:
        """transform（WATERFALL）：链式改写上下文文本。"""
        host.hook("transform").tap(self.name, self.rewrite)
