"""补齐「当前 e2e 从未执行过」的可靠性 / 模型层公开 API 真实调用测试。

纪律：
- 每个用例：输入 → 真实入口 → 精确副作用/返回值断言 → 失败判据（写在 docstring）；
- 不用 isinstance / hasattr / registry.has 当证据、不用反射模板、不用 is not None /
  callable / 恒真式断言；纯函数也断言精确输出；
- 异步统一用 tests/e2e/conftest.py 的 loop + run，每用例独立实例。
"""
from __future__ import annotations

import asyncio
from random import Random
from types import SimpleNamespace
from typing import Any

import pytest

from compose import (
    FaultRecord,
    HookBusError,
    Invocation,
    LLMRequest,
    LLMResponse,
    PortCtx,
    RoutingTarget,
    Usage,
)
from conftest import run
from harness.model.cache import (
    DEFAULT_CACHE_EMBED_DIM,
    ExactCache,
    HashCacheEmbedder,
    InMemoryCacheStore,
    SimilarityCache,
)
from harness.model.capability import ModelCapability
from harness.model.gateway import ModelGateway
from harness.model.registry import InMemoryModelRegistry, ModelEndpoint
from harness.model.structured import (
    MinimalSchemaValidator,
    StructuredOutputValidator,
    _JsonType,
    _OutputMode,
    validate_json_schema,
)
from harness.model.types import (
    CostAttributor,
    CostGuard,
    CostRecord,
    RefuseExecution,
    Reservation,
    StructuredOutputSpec,
    TargetUnresolved,
    UsageLedger,
)
from harness.resilience.breaker import CircuitBreaker, InMemoryBreakerStore
from harness.resilience.defenses import (
    Bulkhead,
    Degradation,
    Idempotency,
    KillSwitch,
    RateLimit,
    RatePermit,
    Watchdog,
    _ExceedPolicy,
    _KillScope,
    failed_target_of,
)
from harness.resilience.executor import ReliabilityExecutor
from harness.resilience.retry import RetryPolicy
from harness.resilience.timeout import Timeout
from harness.resilience.types import (
    BreakerState,
    DegradeLadder,
    DegradeMode,
    DegradeStep,
    DimensionKey,
    FailureClass,
    Jitter,
    RetrySpec,
    _DefaultClassifier,
    backoff_delay,
    classify_failure,
    normalize_code,
    validate_ladder,
    validate_retry_spec,
)
from runtime import Invoke, Result, Terminate


# --------------------------------------------------------------------------- #
# 本地夹具（每用例独立实例，不跨用例共享状态）
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, session_id: str = "s-e2e") -> None:
        self.session_id = session_id


def _inv(
    effect: Any = None, *, sid: str = "s-e2e", meta: dict[str, Any] | None = None
) -> Invocation:
    """构造真实 Invocation（compose.CallContext），effect 缺省为 LLM Invoke。"""
    eff = effect if effect is not None else Invoke(port="llm", payload={"messages": []})
    return Invocation(
        effect=eff,
        ctx=SimpleNamespace(session_id=sid, meta={}),
        session=_Session(sid),
        meta=meta if meta is not None else {},
    )


def _fail(code: str) -> Result:
    return Result.failure(code, f"err:{code}")


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


class _EchoLLM:
    """履约 LLMPort + LLMPortWithUsage：complete/stream 真产出并记录调用与 usage。"""

    name = "echo_llm"

    def __init__(self, answer: str, cost: float = 0.0) -> None:
        self.answer = answer
        self.cost = cost
        self.complete_calls = 0
        self.stream_calls = 0
        self.received: list[list[dict[str, Any]]] = []
        self.last_usage: Usage | None = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.complete_calls += 1
        self.received.append([dict(m) for m in req.messages])
        usage = Usage(prompt_tokens=2, completion_tokens=3, cost_usd=self.cost)
        self.last_usage = usage
        return LLMResponse(content=self.answer, tool_calls=[], usage=usage, model=self.name)

    async def _stream(self, req: LLMRequest, ctx: PortCtx) -> Any:
        self.stream_calls += 1
        self.received.append([dict(m) for m in req.messages])
        for ch in self.answer:
            yield ch
        self.last_usage = Usage(
            prompt_tokens=1, completion_tokens=len(self.answer), cost_usd=self.cost
        )

    def stream(self, req: LLMRequest, ctx: PortCtx) -> Any:
        return self._stream(req, ctx)


class _Provider:
    """最小 Provider（compose.Provider 只要求具名）。"""

    def __init__(self, name: str) -> None:
        self.name = name


def _req(text: str) -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}])


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], usage=Usage(), model="echo")


# =========================================================================== #
# harness.resilience.types（10）
# =========================================================================== #
def test_types_jitter_coerce_exact_members() -> None:
    """输入 "equal"/未知串/枚举成员→Jitter.coerce→精确断言成员身份→失败判据：未知值未回退 FULL 或成员被改写。"""
    assert Jitter.coerce("equal") is Jitter.EQUAL
    assert Jitter.coerce("decorrelated") is Jitter.DECORRELATED
    assert Jitter.coerce("no-such-jitter") is Jitter.FULL
    assert Jitter.coerce(Jitter.NONE) is Jitter.NONE


def test_types_normalize_code_exact_families() -> None:
    """输入 HTTP/符号/未知错误码→normalize_code→精确断言归一化族→失败判据：别名或 5xx 兜底失效。"""
    assert normalize_code("429") == "RATE_LIMITED"
    assert normalize_code(" TOO_MANY_REQUESTS ") == "RATE_LIMITED"
    assert normalize_code("503") == "SERVER_ERROR"
    assert normalize_code("599") == "SERVER_ERROR"
    assert normalize_code("WEIRD_VENDOR_CODE") == "WEIRD_VENDOR_CODE"


def test_types_default_classifier_exact_failure_class() -> None:
    """输入 TIMEOUT 失败信封→_DefaultClassifier.classify→精确断言四字段→失败判据：重试/熔断语义串味。"""
    fc = _DefaultClassifier().classify(_fail("TIMEOUT"), _inv())
    assert fc == FailureClass(True, True, None, "TIMEOUT")


def test_types_classify_failure_custom_classifier_delegates() -> None:
    """输入 500 失败 + 注入分类器→classify_failure→精确返回注入结论→失败判据：未委托给注入策略。"""
    sentinel = FailureClass(True, True, 1.5, "CUSTOM")

    class Custom:
        name = "custom"

        def classify(self, result: Result, ctx: object) -> FailureClass:
            return sentinel

    assert classify_failure(_fail("500"), _inv(), classifier=Custom()) == sentinel


def test_types_classify_failure_default_rate_limited() -> None:
    """输入 429→classify_failure 默认表→精确断言可重试不计熔断→失败判据：429 被误计为对方宕机。"""
    assert classify_failure(_fail("429"), _inv()) == FailureClass(
        True, False, None, "RATE_LIMITED"
    )


def test_types_dimension_key_as_tuple_exact() -> None:
    """输入具名维度→DimensionKey.as_tuple→精确二元组→失败判据：维度顺序或取值被改写。"""
    assert DimensionKey(dim_type="provider", name="openai").as_tuple() == ("provider", "openai")


def test_types_validate_retry_spec_accepts_and_rejects() -> None:
    """输入合法/非法 RetrySpec→validate_retry_spec→精确异常→失败判据：非法参数静默放行。"""
    validate_retry_spec(RetrySpec())  # 合法不抛
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        validate_retry_spec(RetrySpec(max_attempts=0))
    with pytest.raises(ValueError, match="retry storm"):
        validate_retry_spec(RetrySpec(base_delay=0.0, jitter="none"))


def test_types_backoff_delay_none_and_max_exact() -> None:
    """输入指数退避参数→backoff_delay→精确 1/2/4/封顶值→失败判据：指数或上限算错。"""
    spec = RetrySpec(base_delay=1.0, multiplier=2.0, max_delay=10.0, jitter="none")
    rng = Random(0)
    assert backoff_delay(1, spec, rng) == 1.0
    assert backoff_delay(2, spec, rng) == 2.0
    assert backoff_delay(3, spec, rng) == 4.0
    assert backoff_delay(10, spec, rng) == 10.0


def test_types_backoff_delay_jitter_and_retry_after_exact() -> None:
    """输入四种抖动 + Retry-After→backoff_delay→精确回放值→失败判据：抖动算法或背压优先失效。"""
    full = RetrySpec(base_delay=1.0, multiplier=1.0, jitter="full")
    assert backoff_delay(1, full, Random(7)) == pytest.approx(Random(7).uniform(0.0, 1.0))

    equal = RetrySpec(base_delay=2.0, multiplier=1.0, jitter="equal")
    assert backoff_delay(1, equal, Random(7)) == pytest.approx(
        1.0 + Random(7).uniform(0.0, 1.0)
    )

    decor = RetrySpec(base_delay=1.0, multiplier=1.0, max_delay=100.0, jitter="decorrelated")
    assert backoff_delay(1, decor, Random(7), prev_delay=10.0) == pytest.approx(
        10.0 * Random(7).uniform(1.0, 3.0)
    )

    none = RetrySpec(base_delay=0.1, multiplier=1.0, jitter="none")
    assert backoff_delay(1, none, Random(0), retry_after=5.0) == 5.0
    strict = RetrySpec(base_delay=0.1, multiplier=1.0, jitter="none", respect_retry_after=False)
    assert backoff_delay(1, strict, Random(0), retry_after=5.0) == pytest.approx(0.1)


def test_types_ladder_pick_and_validate_exact() -> None:
    """输入可用集/非法阶梯→DegradeLadder.pick + validate_ladder→精确选级与异常→失败判据：落级或校验失效。"""
    ladder = _ladder()
    assert ladder.pick({"primary", "backup"}).target == "primary"
    assert ladder.pick({"backup"}).target == "backup"
    refuse = ladder.pick(set())
    assert refuse.target == "" and refuse.mode is DegradeMode.REFUSE and refuse.quality == 0.0

    validate_ladder(ladder)  # 合法不抛
    cyclic = DegradeLadder(steps=(
        DegradeStep("primary", DegradeMode.REROUTE, 1.0),
        DegradeStep("primary", DegradeMode.REROUTE, 0.5),
        DegradeStep("", DegradeMode.REFUSE, 0.0),
    ))
    with pytest.raises(ValueError, match="cycle"):
        validate_ladder(cyclic)


def test_types_breaker_state_failure_rate_exact() -> None:
    """输入空窗/混合滑窗→BreakerState.failure_rate→精确 0.0/0.5→失败判据：失败率算错或空窗除零。"""
    assert BreakerState().failure_rate() == 0.0
    state = BreakerState(status="closed", failures=2, window=(True, False, True, False))
    assert state.failure_rate() == 0.5


# =========================================================================== #
# harness.resilience.defenses（19）
# =========================================================================== #
def test_defenses_failed_target_of_exact() -> None:
    """输入带模型/空模型/非 Invoke 的 Invocation→failed_target_of→精确注册名→失败判据：目标串被改写。"""
    assert failed_target_of(_inv(Invoke(port="llm", payload={"model": "backup"}))) == "backup"
    assert failed_target_of(_inv(Invoke(port="llm", payload={"model": ""}))) == "primary"
    assert failed_target_of(_inv(Terminate(output="done"))) == "primary"


def test_defenses_exceed_and_kill_scope_coerce_exact() -> None:
    """输入合法/未知字符串→_ExceedPolicy.coerce + _KillScope.coerce→精确回退成员→失败判据：fail-closed/最小权限回退丢失。"""
    assert _ExceedPolicy.coerce("reject") is _ExceedPolicy.REJECT
    assert _ExceedPolicy.coerce("mystery") is _ExceedPolicy.DEGRADE
    assert _KillScope.coerce("global") is _KillScope.GLOBAL
    assert _KillScope.coerce("mystery") is _KillScope.SESSION


def test_defenses_degradation_init_validates_ladder() -> None:
    """输入非法阶梯→Degradation 构造→装配期抛 ValueError→失败判据：坏阶梯被静默接收。"""
    bad = DegradeLadder(steps=(DegradeStep("primary", DegradeMode.REROUTE, 1.0),))
    with pytest.raises(ValueError, match="REFUSE step"):
        Degradation(bad)


def test_defenses_degradation_pick_exact() -> None:
    """输入可用集/None→Degradation.pick→精确选级→失败判据：初始 available 未生效或全无未拒答。"""
    deg = Degradation(_ladder(), available={"primary", "backup"})
    assert deg.pick().target == "primary"
    assert deg.pick({"backup"}).target == "backup"
    assert deg.pick(set()) == DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0)


def test_defenses_degradation_select_shrinks_per_session() -> None:
    """输入失败目标 fault + 两个 session→Degradation.select→逐次剔除且会话隔离→失败判据：目标未写回或跨会话串味。"""
    deg = Degradation(_ladder(), available={"primary", "backup"})
    assert deg.select(FaultRecord("none", ""), session_id="s1") == "primary"
    assert deg.select(FaultRecord("primary_down", "primary"), session_id="s1") == "backup"
    assert deg.select(FaultRecord("none", ""), session_id="s1") == "backup"
    assert deg.select(FaultRecord("none", ""), session_id="s2") == "primary"
    assert deg.select(FaultRecord("backup_down", "backup"), session_id="s1") == "refuse"


def test_defenses_rate_limit_init_and_acquire_admit_then_degrade() -> None:
    """输入 burst=2/rate=0 的注入时钟→RateLimit.acquire→两次 ADMIT 后 DEGRADE→失败判据：令牌桶容量或超限策略错。"""
    clock = [0.0]
    rl = RateLimit(rate=0.0, burst=2, on_exceed="wait", clock=lambda: clock[0])
    assert rl.acquire() is RatePermit.ADMIT
    assert rl.acquire() is RatePermit.ADMIT
    assert rl.acquire() is RatePermit.DEGRADE
    clock[0] = 1.0  # rate=0 不补充
    assert rl.acquire() is RatePermit.DEGRADE


def test_defenses_rate_limit_reject_and_refill_exact() -> None:
    """输入 REJECT 策略与推进时钟→RateLimit.acquire→精确 REJECT/ADMIT→失败判据：超限策略或补充速率错。"""
    clock = [0.0]
    rl = RateLimit(rate=1.0, burst=2, on_exceed="reject", clock=lambda: clock[0])
    assert rl.acquire() is RatePermit.ADMIT
    assert rl.acquire() is RatePermit.ADMIT
    assert rl.acquire() is RatePermit.REJECT
    clock[0] = 2.0
    assert rl.acquire() is RatePermit.ADMIT


def test_defenses_bulkhead_acquire_release_exact() -> None:
    """输入容量 1 的池→Bulkhead.acquire/release→精确 True/False 序列→失败判据：槽位未占用或未释放。"""
    bh = Bulkhead(pools={"p": 1}, pool="p")
    assert bh.acquire("p") is True
    assert bh.acquire("p") is False
    bh.release("p")
    assert bh.acquire("p") is True
    bh.release("p")
    bh.release("p")  # 下限 0
    assert bh.acquire("p") is True


def test_defenses_bulkhead_run_in_slot_rejects_when_full(loop: Any) -> None:
    """输入满池 + 成功续延→Bulkhead.run_in_slot→占用/释放与精确失败码→失败判据：槽位泄漏或满池未拒绝。"""
    bh = Bulkhead(pools={"p": 1}, on_full="reject", pool="p")
    inv = _inv()
    inside: list[bool] = []

    async def nxt(i: Invocation) -> Result:
        inside.append(bh.acquire("p"))  # 占用中应拿不到第二个槽
        return Result.success("done")

    assert run(bh.run_in_slot(inv, nxt), loop) == Result.success("done")
    assert inside == [False]
    assert bh.acquire("p") is True  # finally 已释放


def test_defenses_bulkhead_run_in_slot_degrade_writes_fault(loop: Any) -> None:
    """输入零容量 + DEGRADE 策略→Bulkhead.run_in_slot→写 fault 且仍执行续延→失败判据：故障记录缺失或续延被吞。"""
    bh = Bulkhead(pools={"p": 0}, on_full="degrade", pool="p")
    inv = _inv(Invoke(port="llm", payload={"model": "backup"}))
    calls: list[str] = []

    async def nxt(i: Invocation) -> Result:
        calls.append("called")
        return Result.success("ok")

    assert run(bh.run_in_slot(inv, nxt), loop) == Result.success("ok")
    assert calls == ["called"]
    assert inv.fault() == FaultRecord(reason="bulkhead_full", failed_target="backup")


def test_defenses_idempotency_init_and_check_and_mark_ttl() -> None:
    """输入注入 store/clock/TTL→Idempotency.check_and_mark→首见 False/重见 True/过期 False→失败判据：去重或淘汰失效。"""
    store: dict[str, float] = {}
    clock = [0.0]
    idem = Idempotency(store=store, ttl=10.0, clock=lambda: clock[0])
    assert idem.check_and_mark("k") is False
    assert store == {"k": 0.0}
    clock[0] = 5.0
    assert idem.check_and_mark("k") is True
    clock[0] = 11.0
    assert idem.check_and_mark("k") is False
    assert store == {"k": 11.0}


def test_defenses_watchdog_init_inspect_repeat_and_abab() -> None:
    """输入重复/A-B 交替/普通序列→Watchdog.inspect→精确 True/False→失败判据：循环模式未识别或误报。"""
    repeat = Watchdog(window=10, repeat_k=3, detect_abab=False)
    assert repeat.inspect("tool") is False
    assert repeat.inspect("tool") is False
    assert repeat.inspect("tool") is True

    abab = Watchdog(window=10, repeat_k=99, detect_abab=True)
    assert abab.inspect("a") is False
    assert abab.inspect("b") is False
    assert abab.inspect("a") is False
    assert abab.inspect("b") is True

    plain = Watchdog(window=10, repeat_k=3, detect_abab=True)
    assert [plain.inspect(k) for k in ("a", "b", "c")] == [False, False, False]


def test_defenses_watchdog_detect_respects_kinds_and_effect() -> None:
    """输入白名单内 Invoke/外部 Effect/名单外 port→Watchdog.detect→精确布尔→失败判据：白名单或类型过滤失效。"""
    wd = Watchdog(window=10, repeat_k=3, kinds={"tool"})
    tool_inv = _inv(Invoke(port="tool", payload={}))
    assert wd.detect(tool_inv) is False
    assert wd.detect(tool_inv) is False
    assert wd.detect(tool_inv) is True
    assert wd.detect(_inv(Terminate(output="x"))) is False
    assert wd.detect(_inv(Invoke(port="llm", payload={}))) is False


def test_defenses_kill_switch_is_tripped_exact_keys() -> None:
    """输入 session/global 标志→KillSwitch.is_tripped→精确布尔与作用域回退→失败判据：全局标志未生效或作用域串味。"""
    flags = {"kill:session": True}
    ks = KillSwitch(flag_store=flags, scope="session")
    assert ks.is_tripped("s1") is True
    flags.clear()
    assert ks.is_tripped("s1") is False
    flags["kill:global"] = True
    assert ks.is_tripped("s1") is True

    flags.clear()
    unknown_scope = KillSwitch(flag_store=flags, scope="mystery")
    flags["kill:session"] = True  # 未知值回退 SESSION
    assert unknown_scope.is_tripped("s1") is True


# =========================================================================== #
# harness.resilience.breaker（10）
# =========================================================================== #
def test_breaker_store_init_load_missing(loop: Any) -> None:
    """输入空存储+未知维度→InMemoryBreakerStore.load→精确 None→失败判据：空存储返回了幽灵状态。"""
    store = InMemoryBreakerStore()
    assert run(store.load(("provider", "nobody")), loop) is None


def test_breaker_store_save_and_load_exact(loop: Any) -> None:
    """输入具名快照→InMemoryBreakerStore.save/load→精确同一状态与覆盖语义→失败判据：快照未被保存或被改写。"""
    store = InMemoryBreakerStore()
    dim = ("provider", "openai")
    first = BreakerState(status="open", failures=3, window=(True, True, True), opened_at=5.0)
    run(store.save(dim, first), loop)
    assert run(store.load(dim), loop) == first
    second = BreakerState(status="closed", failures=0)
    run(store.save(dim, second), loop)
    assert run(store.load(dim), loop) == second


def test_breaker_init_and_state_default_exact(loop: Any) -> None:
    """输入自定义熔断参数→CircuitBreaker 构造 + state→精确默认 CLOSED 快照→失败判据：初始态或维度归一错。"""
    cb = CircuitBreaker(minimum_samples=2, failure_rate=0.5, open_cooldown_s=1.0)
    assert run(cb.state(("provider", "openai")), loop) == BreakerState()
    assert run(cb.state("not-a-tuple"), loop) == BreakerState()  # 归一到空维度


def test_breaker_check_open_in_cooldown_exact(loop: Any) -> None:
    """输入 2 次失败（样本阈值 2）→CircuitBreaker.record/check→精确 breaker_open→失败判据：OPEN 未触发或冷却失效。"""
    now = [0.0]
    cb = CircuitBreaker(
        minimum_samples=2, failure_rate=0.5, open_cooldown_s=30.0, clock=lambda: now[0]
    )
    dim = ("provider", "openai")
    run(cb.record(_fail("500"), dim), loop)
    run(cb.record(_fail("500"), dim), loop)
    assert run(cb.check(dim), loop) == "breaker_open"
    now[0] = 30.0
    assert run(cb.check(dim), loop) is None  # 冷却边界届满放行探测


def test_breaker_maybe_half_open_exact_snapshot(loop: Any) -> None:
    """输入 OPEN 快照 + 推进时钟→CircuitBreaker.maybe_half_open→精确 HALF_OPEN 状态→失败判据：探测窗口未重置。"""
    now = [0.0]
    cb = CircuitBreaker(
        minimum_samples=1, failure_rate=0.5, open_cooldown_s=30.0, clock=lambda: now[0]
    )
    dim = ("provider", "openai")
    run(cb.record(_fail("500"), dim), loop)
    assert run(cb.state(dim), loop).status == "open"
    now[0] = 31.0
    run(cb.maybe_half_open(dim), loop)
    assert run(cb.state(dim), loop) == BreakerState(
        status="half_open", failures=1, window=(), half_open_successes=0, opened_at=None
    )


def test_breaker_record_count_false_and_window_exact(loop: Any) -> None:
    """输入 count=False 与混合结果→CircuitBreaker.record→精确窗口/失败数→失败判据：对方保护被误计或滑窗错。"""
    cb = CircuitBreaker(minimum_samples=5, failure_rate=0.5)
    dim = ("provider", "openai")
    run(cb.record(_fail("429"), dim, count=False), loop)
    assert run(cb.state(dim), loop) == BreakerState()
    run(cb.record(_fail("500"), dim), loop)
    run(cb.record(Result.success("ok"), dim), loop)
    state = run(cb.state(dim), loop)
    assert state.window == (True, False)
    assert state.failures == 1
    assert state.failure_rate() == 0.5


def test_breaker_check_half_open_probe_limit_exact(loop: Any) -> None:
    """输入 HALF_OPEN 且探测满额→CircuitBreaker.check→精确 breaker_half_open→失败判据：半开超额未限流。"""
    store = InMemoryBreakerStore()
    cb = CircuitBreaker(half_open_max_probes=1, store=store)
    dim = ("provider", "openai")
    run(store.save(dim, BreakerState(status="half_open", window=(True,))), loop)
    assert run(cb.check(dim), loop) == "breaker_half_open"


def test_breaker_permit_dimension_isolation_exact(loop: Any) -> None:
    """输入不同模型的 Invocation→CircuitBreaker.permit→仅故障维度被拒→失败判据：维度隔离失效。"""
    cb = CircuitBreaker(
        minimum_samples=1, failure_rate=0.5, open_cooldown_s=30.0,
        dimension_of=lambda inv: ("provider", str(inv.effect.payload.get("model") or "primary")),
    )
    run(cb.record(_fail("500"), ("provider", "primary")), loop)
    primary_inv = _inv(Invoke(port="llm", payload={"model": "primary"}))
    backup_inv = _inv(Invoke(port="llm", payload={"model": "backup"}))
    assert run(cb.permit(primary_inv), loop) == "breaker_open"
    assert run(cb.permit(backup_inv), loop) is None


def test_breaker_observe_outcome_records_by_invocation(loop: Any) -> None:
    """输入带维度的 Invocation + 结果→CircuitBreaker.observe_outcome→精确滑窗写入→失败判据：记账维度或结果极性错。"""
    cb = CircuitBreaker(
        minimum_samples=3, failure_rate=0.5,
        dimension_of=lambda inv: ("provider", str(inv.effect.payload.get("model") or "primary")),
    )
    inv = _inv(Invoke(port="llm", payload={"model": "backup"}))
    run(cb.observe_outcome(inv, _fail("500")), loop)
    run(cb.observe_outcome(inv, Result.success("ok")), loop)
    state = run(cb.state(("provider", "backup")), loop)
    assert state.window == (True, False)
    assert state.failures == 1
    assert run(cb.state(("provider", "primary")), loop) == BreakerState()


# =========================================================================== #
# harness.resilience.retry / timeout / executor（5）
# =========================================================================== #
def test_retry_policy_init_rejects_storm_config() -> None:
    """输入 base_delay=0/jitter=none→RetryPolicy 构造→精确 ValueError→失败判据：重试风暴配置被放行。"""
    with pytest.raises(ValueError, match="retry storm"):
        RetryPolicy(base_delay=0.0, jitter="none")


def test_retry_run_with_retries_replays_and_backoff(loop: Any) -> None:
    """输入 500/500/成功 + 记录 sleeper→run_with_retries→3 次调用、退避 [0.1,0.2] 成功→失败判据：重放次数或退避错。"""
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    policy = RetryPolicy(
        max_attempts=3, base_delay=0.1, multiplier=2.0, jitter="none", sleeper=sleeper
    )
    inv = _inv(Invoke(port="llm", payload={"model": "primary"}))
    outcomes = [_fail("500"), _fail("500"), Result.success({"v": 7})]
    calls = 0

    async def nxt(i: Invocation) -> Result:
        nonlocal calls
        result = outcomes[calls]
        calls += 1
        return result

    result = run(policy.run_with_retries(inv, nxt), loop)
    assert result == Result.success({"v": 7})
    assert calls == 3
    assert delays == [0.1, 0.2]
    assert inv.meta["retry_classified"] == "SERVER_ERROR"
    assert inv.fault() is None


def test_retry_run_with_retries_exhausted_writes_fault(loop: Any) -> None:
    """输入持续失败→run_with_retries→写 retry_exhausted 且带 failed_target→失败判据：耗尽故障缺目标名。"""
    async def no_sleep(_delay: float) -> None:
        return None

    policy = RetryPolicy(
        max_attempts=2, base_delay=0.01, jitter="none", sleeper=no_sleep,
        target_of=lambda inv: str(inv.effect.payload.get("model") or "primary"),
    )
    inv = _inv(Invoke(port="llm", payload={"model": "backup"}))
    calls = 0

    async def nxt(i: Invocation) -> Result:
        nonlocal calls
        calls += 1
        return _fail("500")

    result = run(policy.run_with_retries(inv, nxt), loop)
    assert result == _fail("500")
    assert calls == 2
    assert inv.fault() == FaultRecord(reason="retry_exhausted", failed_target="backup")


def test_retry_run_with_retries_idempotency_guard(loop: Any) -> None:
    """输入无幂等键的 TOOL 副作用→run_with_retries→精确 IDEMPOTENCY_REQUIRED 且只调一次→失败判据：副作用被重放。"""
    policy = RetryPolicy(max_attempts=3, base_delay=0.01, jitter="none")
    inv = _inv(Invoke(port="tool", payload={}))
    calls = 0

    async def nxt(i: Invocation) -> Result:
        nonlocal calls
        calls += 1
        return _fail("500")

    result = run(policy.run_with_retries(inv, nxt), loop)
    assert result.error is not None
    assert (result.error.code, result.error.message) == (
        "IDEMPOTENCY_REQUIRED", "idempotency_required for side-effect retry"
    )
    assert calls == 1


def test_timeout_init_rejects_nonpositive() -> None:
    """输入 timeout=0→Timeout 构造→精确 ValueError→失败判据：非法时限被放行。"""
    with pytest.raises(ValueError, match="timeout must be positive"):
        Timeout(timeout=0.0)


def test_timeout_run_with_deadline_pass_through(loop: Any) -> None:
    """输入未超时续延→run_with_deadline→精确透传成功 Result 且无 fault→失败判据：正常调用被误判超时。"""
    timeout = Timeout(timeout=1.0)
    inv = _inv()

    async def nxt(i: Invocation) -> Result:
        return Result.success("passthrough")

    result = run(timeout.run_with_deadline(inv, nxt), loop)
    assert result == Result.success("passthrough")
    assert inv.meta == {}


def test_timeout_run_with_deadline_timeout_writes_fault(loop: Any) -> None:
    """输入永不完成的续延→run_with_deadline→TIMEOUT 失败 + 具名 fault→失败判据：超时未写故障或丢了 failed_target。"""
    timeout = Timeout(
        timeout=0.01, target_of=lambda inv: str(inv.effect.payload.get("model") or "primary")
    )
    inv = _inv(Invoke(port="llm", payload={"model": "backup"}))

    async def stuck(i: Invocation) -> Result:
        await asyncio.Event().wait()
        return Result.success("unreachable")

    result = run(timeout.run_with_deadline(inv, stuck), loop)
    assert result.error is not None
    assert (result.error.code, result.error.message) == ("TIMEOUT", "invoke exceeded 0.01s")
    assert inv.fault() == FaultRecord(reason="timeout", failed_target="backup")


def test_executor_as_call_killswitch_rejects(loop: Any) -> None:
    """输入置位 killswitch + as_call→ReliabilityExecutor 前置拒绝→精确失败信封/通知/核心零调用→失败判据：核心被调用或通知缺失。"""
    calls: list[Invocation] = []
    notes: list[tuple[str, dict[str, object]]] = []

    async def core(inv: Invocation) -> Result:
        calls.append(inv)
        return Result.success("ok")

    ks = KillSwitch(flag_store={"kill:session": True}, scope="session")
    executor = ReliabilityExecutor(
        core, kill_switch=ks, notify=lambda phase, payload: notes.append((phase, payload))
    )
    ctx = SimpleNamespace(session_id="s-kill", meta={})
    result = run(executor.as_call()(
        Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]}), ctx
    ), loop)
    assert result == Result.failure("INVOKE_REJECTED", "killswitch")
    assert calls == []
    assert notes == [("invoke_rejected", {"reason": "killswitch", "session_id": "s-kill"})]


def test_executor_run_invocation_reroutes_to_backup(loop: Any) -> None:
    """输入主后端失败+降级阶梯→run_invocation→真选备用后端并返回其成功→失败判据：未重路由或 typed target 丢失。"""
    seen: list[str | None] = []

    async def core(inv: Invocation) -> Result:
        target = inv.routing_target()
        seen.append(target.target if target is not None else None)
        if target is None:
            inv.write_fault(FaultRecord(reason="primary_down", failed_target="primary"))
            return Result.failure("500", "primary down")
        return Result.success(target.target)

    notes: list[tuple[str, dict[str, object]]] = []
    executor = ReliabilityExecutor(
        core,
        degradation=Degradation(_ladder(), available={"primary", "backup"}),
        notify=lambda phase, payload: notes.append((phase, payload)),
    )
    inv = _inv(sid="s-reroute")
    result = run(executor.run_invocation(inv), loop)
    assert result == Result.success("backup")
    assert seen == [None, "backup"]
    assert inv.routing_target() == RoutingTarget(target="backup", reason="primary_down")
    assert notes == [("invoke_rejected", {"reason": "primary_down", "session_id": "s-reroute"})]


# =========================================================================== #
# harness.model.types（14）
# =========================================================================== #
def test_model_types_refuse_execution_to_result() -> None:
    """输入拒答原因→RefuseExecution.to_result→精确 REFUSED 失败信封→失败判据：错误码或消息被改写。"""
    assert RefuseExecution("ladder exhausted").to_result() == Result.failure(
        "REFUSED", "ladder exhausted"
    )


def test_model_types_target_unresolved_to_result() -> None:
    """输入未注册目标→TargetUnresolved.to_result→精确 TARGET_UNRESOLVED 失败→失败判据：静默回退默认模型被掩盖。"""
    assert TargetUnresolved("ghost").to_result() == Result.failure(
        "TARGET_UNRESOLVED", "degrade target not registered: ghost"
    )


def _record(cost: float, *, run: str = "r1", agent: str = "a1", user: str = "u1",
            model: str = "m1") -> CostRecord:
    return CostRecord(
        run_id=run, agent_id=agent, user_id=user, model=model,
        usage=Usage(prompt_tokens=2, completion_tokens=3, cost_usd=cost),
    )


def test_model_types_usage_ledger_total_exact() -> None:
    """输入两条成本记录→UsageLedger.total→精确合计→失败判据：累计金额或记录数错。"""
    ledger = UsageLedger()
    ledger.record(_record(0.125))
    ledger.record(_record(0.25))
    assert ledger.total() == 0.375
    assert ledger.count == 2


def test_model_types_usage_ledger_by_exact() -> None:
    """输入不同模型成本→UsageLedger.by→精确维度值与未注册维度 0.0→失败判据：维度串味。"""
    ledger = UsageLedger()
    ledger.record(_record(0.5, model="m1"))
    ledger.record(_record(0.25, model="m2"))
    assert ledger.by("model", "m1") == 0.5
    assert ledger.by("model", "m2") == 0.25
    assert ledger.by("model", "missing") == 0.0


def test_model_types_cost_attributor_named_dimensions_exact() -> None:
    """输入多维成本记录→CostAttributor.run_cost/user_cost/model_cost→精确各维金额→失败判据：维度归因错。"""
    attr = CostAttributor()
    attr.record(_record(0.125, run="run-a", user="user-a", model="model-a"))
    attr.record(_record(0.25, run="run-b", user="user-a", model="model-b"))
    assert attr.run_cost("run-a") == 0.125
    assert attr.run_cost("run-b") == 0.25
    assert attr.user_cost("user-a") == 0.375
    assert attr.model_cost("model-a") == 0.125
    assert attr.model_cost("model-b") == 0.25


def test_model_types_cost_attributor_snapshot_exact() -> None:
    """输入三条记录→CostAttributor.snapshot→精确 total/records 字典→失败判据：快照口径错。"""
    attr = CostAttributor()
    for cost in (0.125, 0.25, 0.5):
        attr.record(_record(cost))
    assert attr.snapshot() == {"total": 0.875, "records": 3}


def test_model_types_cost_guard_allow_exact() -> None:
    """输入预算 1.0 的评估值→CostGuard.allow→精确 True/False（含预留）→失败判据：硬闸判断错。"""
    guard = CostGuard(max_cost_usd=1.0)
    assert guard.allow(1.0) is True
    assert guard.allow(1.01) is False
    guard.reserve(0.4)
    assert guard.allow(0.6) is True
    assert guard.allow(0.61) is False


def test_model_types_cost_guard_charge_exact() -> None:
    """输入实际成本→CostGuard.charge→精确 spent 与 remaining→失败判据：扣费未生效。"""
    guard = CostGuard(max_cost_usd=1.0)
    guard.charge(0.25)
    guard.charge(0.125)
    assert guard.spent == 0.375
    assert guard.remaining() == 0.625


def test_model_types_cost_guard_reserve_exact() -> None:
    """输入估算成本→CostGuard.reserve→精确凭证/序号/余量/超预算 None→失败判据：预留未占用额度。"""
    guard = CostGuard(max_cost_usd=1.0)
    assert guard.reserve(0.25) == Reservation(id=1, amount=0.25)
    assert guard.reserve(0.5) == Reservation(id=2, amount=0.5)
    assert guard.remaining() == 0.25
    assert guard.reserve(0.26) is None


def test_model_types_cost_guard_commit_exact() -> None:
    """输入预留 0.25 + 实际 0.1→CostGuard.commit→按实结算且重复结清无效→失败判据：差额未退或重复扣费。"""
    guard = CostGuard(max_cost_usd=1.0)
    r = guard.reserve(0.25)
    assert r is not None
    guard.commit(r, 0.1)
    assert guard.spent == 0.1
    assert guard.remaining() == 0.9
    guard.commit(r, 0.1)
    assert guard.spent == 0.1
    assert guard.remaining() == 0.9


def test_model_types_cost_guard_cancel_exact() -> None:
    """输入预留后取消→CostGuard.cancel→额度释放且不记支出→失败判据：预留悬空或误记支出。"""
    guard = CostGuard(max_cost_usd=1.0)
    r = guard.reserve(0.25)
    assert r is not None
    guard.cancel(r)
    assert guard.spent == 0.0
    assert guard.remaining() == 1.0
    guard.cancel(r)
    assert guard.remaining() == 1.0


def test_model_types_cost_guard_remaining_exact() -> None:
    """输入已花+未结清预留→CostGuard.remaining→精确扣减结果→失败判据：余量漏扣预留。"""
    guard = CostGuard(max_cost_usd=2.0)
    guard.charge(0.5)
    assert guard.reserve(0.25) == Reservation(id=1, amount=0.25)
    assert guard.remaining() == 1.25


# =========================================================================== #
# harness.model.structured（11）
# =========================================================================== #
def test_structured_json_type_coerce_exact() -> None:
    """输入合法/非法 type 值→_JsonType.coerce→精确枚举或 None→失败判据：坏 schema 类型被静默放过。"""
    assert _JsonType.coerce("object") is _JsonType.OBJECT
    assert _JsonType.coerce("integer") is _JsonType.INTEGER
    assert _JsonType.coerce("str") is None
    assert _JsonType.coerce(123) is None


def test_structured_output_mode_coerce_exact() -> None:
    """输入合法/未知 mode→_OutputMode.coerce→精确成员或 REPAIR 回退→失败判据：未知模式未保守回退。"""
    assert _OutputMode.coerce("prompt") is _OutputMode.PROMPT
    assert _OutputMode.coerce("constrained") is _OutputMode.CONSTRAINED
    assert _OutputMode.coerce("mystery") is _OutputMode.REPAIR


def test_structured_validate_json_schema_exact_errors() -> None:
    """输入坏 JSON 文本/缺字段/类型错→validate_json_schema→精确错误列表→失败判据：错误文案或顺序不符。"""
    schema = {
        "type": "object",
        "required": ["x", "z"],
        "properties": {"y": {"type": "string"}},
    }
    assert validate_json_schema({"y": 1}, schema) == [
        "$.x: required field missing",
        "$.z: required field missing",
        "$.y: expected string, got int",
    ]
    assert validate_json_schema("not json", schema) == ["invalid JSON text: 'not json'"]


def test_structured_validate_json_schema_enum_minimum_and_bad_schema() -> None:
    """输入 enum/minimum/非法 schema→validate_json_schema→精确错误串→失败判据：约束失效或坏 schema 静默跳过。"""
    assert validate_json_schema('"c"', {"type": "string", "enum": ["a", "b"]}) == [
        "$: value 'c' not in enum ['a', 'b']"
    ]
    assert validate_json_schema(2, {"type": "integer", "minimum": 3}) == [
        "$: 2 < minimum 3"
    ]
    assert validate_json_schema({"a": 1}, {"type": "str"}) == [
        "$: invalid schema type 'str'"
    ]


def test_structured_minimal_validator_exact() -> None:
    """输入合法/非法值→MinimalSchemaValidator.validate→精确空列表/错误串→失败判据：默认校验器口径不一致。"""
    validator = MinimalSchemaValidator()
    assert validator.validate({"n": 1}, {"type": "object"}) == []
    assert validator.validate({"n": "x"}, {
        "type": "object", "properties": {"n": {"type": "integer"}}
    }) == ["$.n: expected integer, got str"]


def test_structured_validator_init_contract_checks() -> None:
    """输入非法注入物/空名 validator→StructuredOutputValidator 构造→精确异常→失败判据：契约校验缺失。"""
    spec = StructuredOutputSpec(schema={"type": "object"})
    with pytest.raises(TypeError, match="SchemaValidator protocol"):
        StructuredOutputValidator(spec, validator=object())  # type: ignore[arg-type]

    class Nameless:
        name = ""
        def validate(self, value: object, schema: dict[str, Any]) -> list[str]:
            return []

    with pytest.raises(ValueError, match="validator.name must be non-empty"):
        StructuredOutputValidator(spec, validator=Nameless())  # type: ignore[arg-type]


def test_structured_validator_properties_exact() -> None:
    """输入 repair/constrained 规格→should_repair/max_repair/schema_validator→精确属性→失败判据：模式口径错。"""
    injected = MinimalSchemaValidator()
    repair_spec = StructuredOutputSpec(schema={"type": "object"}, mode="repair", max_repair=3)
    repair = StructuredOutputValidator(repair_spec, validator=injected)
    assert repair.should_repair is True
    assert repair.max_repair == 3
    assert repair.schema_validator is injected

    constrained = StructuredOutputValidator(
        StructuredOutputSpec(schema={"type": "object"}, mode="constrained")
    )
    assert constrained.should_repair is False
    assert constrained.max_repair == 2


def test_structured_validator_validate_exact_parsed_and_validity() -> None:
    """输入合法/非法 JSON 文本→StructuredOutputValidator.validate→精确解析值与布尔→失败判据：非法输出被标合法。"""
    spec = StructuredOutputSpec(schema={
        "type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}
    })
    validator = StructuredOutputValidator(spec)
    assert validator.validate('{"x": 7}') == ({"x": 7}, True)
    assert validator.validate('{"x": "bad"}') == (None, False)


def test_structured_validator_check_exact_errors() -> None:
    """输入非法输出→StructuredOutputValidator.check→精确错误列表→失败判据：校验口径与修复循环不一致。"""
    spec = StructuredOutputSpec(schema={
        "type": "object", "properties": {"x": {"type": "integer"}}
    })
    validator = StructuredOutputValidator(spec)
    assert validator.check('{"x": "bad"}') == ["$.x: expected integer, got str"]


def test_structured_validator_build_repair_prompt_exact() -> None:
    """输入坏输出与两条错误→build_repair_prompt→精确逐行提示串→失败判据：错误未附给模型。"""
    validator = StructuredOutputValidator(StructuredOutputSpec(schema={"type": "object"}))
    assert validator.build_repair_prompt("bad", ["e1", "e2"]) == (
        "Your previous output failed schema validation.\n"
        "Original output: bad\n"
        "Validation errors:\n"
        "  - e1\n"
        "  - e2\n"
        "Return a corrected JSON object that satisfies the schema."
    )


# =========================================================================== #
# harness.model.cache（9）
# =========================================================================== #
def test_cache_store_delete_exact(loop: Any) -> None:
    """输入 put 后 delete→InMemoryCacheStore.get→精确 None→失败判据：删除未生效。"""
    store = InMemoryCacheStore()
    run(store.put("a", "1"), loop)
    assert run(store.get("a"), loop) == "1"
    run(store.delete("a"), loop)
    assert run(store.get("a"), loop) is None
    run(store.delete("missing"), loop)  # 不存在静默
    assert run(store.get("missing"), loop) is None


def test_cache_store_scan_exact(loop: Any) -> None:
    """输入两种前缀条目→InMemoryCacheStore.scan→精确匹配列表→失败判据：前缀过滤失效。"""
    store = InMemoryCacheStore()
    run(store.put("cache:x:1", "a"), loop)
    run(store.put("cache:y:1", "b"), loop)
    assert run(store.scan("cache:x:"), loop) == [("cache:x:1", "a")]
    assert run(store.scan("cache:"), loop) == [("cache:x:1", "a"), ("cache:y:1", "b")]


def test_cache_embedder_init_and_embed_exact(loop: Any) -> None:
    """输入维度 0 / 空文本 / 重复文本→HashCacheEmbedder 构造与 embed→精确异常与向量→失败判据：非确定性或维度错。"""
    with pytest.raises(ValueError, match="dim must be a positive integer"):
        HashCacheEmbedder(dim=0)

    embedder = HashCacheEmbedder(dim=8)
    assert run(embedder.embed(""), loop) == (0.0,) * 8
    vec = run(embedder.embed("ab"), loop)
    assert len(vec) == 8
    assert vec == run(embedder.embed("ab"), loop)
    assert sum(v * v for v in vec) == pytest.approx(1.0)

    default = HashCacheEmbedder()
    assert len(run(default.embed("中文文本"), loop)) == DEFAULT_CACHE_EMBED_DIM


def test_cache_exact_cache_invalidate_exact(loop: Any) -> None:
    """输入同请求 put/get/invalidate→ExactCache→精确命中/未命中计数与响应→失败判据：失效未生效。"""
    cache = ExactCache()
    request = _req("exact me")
    response = _resp("exact-answer")
    assert run(cache.get(request), loop) is None
    run(cache.put(request, response), loop)
    hit = run(cache.get(request), loop)
    assert hit is not None and hit.content == "exact-answer"
    assert (cache.hits, cache.misses) == (1, 1)
    run(cache.invalidate(request), loop)
    assert run(cache.get(request), loop) is None
    assert (cache.hits, cache.misses) == (1, 2)


def test_cache_similarity_init_rejects_bad_threshold_and_text_mode(loop: Any) -> None:
    """输入非法阈值 / 显式 None embedder→SimilarityCache 构造与存取→精确异常与命中→失败判据：降级文本相似度失效。"""
    with pytest.raises(ValueError, match="threshold must be in"):
        SimilarityCache(threshold=0.0)

    cache = SimilarityCache(threshold=0.9, embedder=None)
    request = _req("hello world")
    run(cache.put(request, _resp("text-mode")), loop)
    hit = run(cache.get(request), loop)
    assert hit is not None and hit.content == "text-mode"


def test_cache_similarity_put_get_exact(loop: Any) -> None:
    """输入语义相同/不同请求→SimilarityCache.put/get→精确命中内容与计数→失败判据：相似度阈值失效。"""
    cache = SimilarityCache(threshold=0.99)
    request = _req("alpha beta gamma")
    run(cache.put(request, _resp("semantic-answer")), loop)
    hit = run(cache.get(request), loop)
    assert hit is not None and hit.content == "semantic-answer"
    assert (cache.hits, cache.misses) == (1, 0)
    assert run(cache.get(_req("unrelated content")), loop) is None
    assert (cache.hits, cache.misses) == (1, 1)


def test_cache_similarity_invalidate_exact(loop: Any) -> None:
    """输入已缓存请求→SimilarityCache.invalidate→精确语义失效结果→失败判据：旧响应仍被命中。"""
    cache = SimilarityCache(threshold=0.99)
    request = _req("invalidate semantic")
    run(cache.put(request, _resp("old-answer")), loop)
    hit = run(cache.get(request), loop)
    assert hit is not None and hit.content == "old-answer"
    run(cache.invalidate(request), loop)
    assert run(cache.get(request), loop) is None
    assert cache.misses == 1


# =========================================================================== #
# harness.model.registry（5）
# =========================================================================== #
def test_registry_register_endpoint_and_provider_exact() -> None:
    """输入端点参数/Provider→InMemoryModelRegistry.register→精确解析结果→失败判据：两种形态未分开落表。"""
    reg = InMemoryModelRegistry()
    reg.register("ep", base_url="https://x/v1", api_key="secret", model="m-x")
    assert reg.resolve("ep") == ModelEndpoint(
        name="ep", base_url="https://x/v1", api_key="secret", model="m-x"
    )
    provider = _Provider("p1")
    reg.register("p1", provider)
    assert reg.resolve_provider("p1") is provider


def test_registry_register_provider_idempotent_and_conflict() -> None:
    """输入同实例/异实例重复注册→register_provider→精确幂等与 HookBusError→失败判据：冲突被静默覆盖。"""
    reg = InMemoryModelRegistry()
    provider = _Provider("p")
    reg.register_provider("p", provider)
    reg.register_provider("p", provider)
    assert reg.resolve_provider("p") is provider
    with pytest.raises(HookBusError, match="different instance already registered for provider: p"):
        reg.register_provider("p", _Provider("other"))


def test_registry_resolve_priority_and_missing_exact() -> None:
    """输入同名端点+Provider→resolve→精确 Provider 优先与未注册 None→失败判据：优先级错或幽灵解析。"""
    reg = InMemoryModelRegistry()
    reg.register("dual", base_url="u", api_key="k", model="m")
    provider = _Provider("dual-provider")
    reg.register_provider("dual", provider)
    assert reg.resolve("dual") is provider
    assert reg.resolve("missing") is None


def test_registry_resolve_provider_exact() -> None:
    """输入已注册/未注册名字→resolve_provider→精确实例与 None→失败判据：provider 与端点表串味。"""
    reg = InMemoryModelRegistry()
    provider = _Provider("pv")
    reg.register_provider("pv", provider)
    reg.register("endpoint-only", base_url="u", api_key="k", model="m")
    assert reg.resolve_provider("pv") is provider
    assert reg.resolve_provider("endpoint-only") is None


def test_registry_has_exact_coverage() -> None:
    """输入端点/后端/provider 三类注册→has→精确 True/False→失败判据：某类注册未被 has 覆盖。"""
    reg = InMemoryModelRegistry()
    llm = _EchoLLM("x")
    reg.register("ep", base_url="u", api_key="k", model="m")
    reg.register_llm("llm1", llm)
    reg.register_provider("pv", _Provider("pv"))
    assert reg.has("ep") is True
    assert reg.has("llm1") is True
    assert reg.has("pv") is True
    assert reg.has("nobody") is False
    assert reg.resolve_llm("llm1") is llm  # 行为佐证，不只靠 has


# =========================================================================== #
# harness.model.capability（5）
# =========================================================================== #
def test_capability_build_program_exact_effects() -> None:
    """输入 ModelCapability→build→Program 真实产出 Invoke+Terminate→精确 Effect 序列→失败判据：外壳 port 或 payload 错。"""
    cap = ModelCapability(llm=_EchoLLM("x"))
    program = cap.build()
    effects = list(program)
    assert effects == [
        Invoke(port="llm", payload={"layer": "model"}),
        Terminate(output=None),
    ]


def test_capability_llm_property_exact_identity() -> None:
    """输入具名 LLM→llm 属性→精确同一实例→失败判据：默认后端被替换。"""
    llm = _EchoLLM("identity")
    assert ModelCapability(llm=llm).llm is llm


def test_capability_registry_property_resolves_primary() -> None:
    """输入 LLM→registry 属性→精确 primary 后端解析→失败判据：默认注册名缺失或换了实例。"""
    llm = _EchoLLM("primary")
    cap = ModelCapability(llm=llm)
    assert cap.registry.resolve_llm("primary") is llm


def test_capability_dispatch_calls_llm(loop: Any) -> None:
    """输入 Invoke + PortCtx→ModelCapability.dispatch→真调 LLM 并返回成功 Result→失败判据：dispatch 空转或结果错。"""
    llm = _EchoLLM("dispatch-ok")
    cap = ModelCapability(llm=llm)
    effect = Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    result = run(cap.dispatch()(effect, PortCtx(session_id="s-dispatch", run_id="r")), loop)
    assert result.ok is True
    assert result.data["content"] == "dispatch-ok"
    assert llm.complete_calls == 1
    assert llm.received == [[{"role": "user", "content": "hi"}]]


def test_capability_add_backend_switches_gateway(loop: Any) -> None:
    """输入 add_backend 注册的备用后端→gateway.complete(degrade_target)→真切后端→失败判据：降级目标未生效。"""
    primary = _EchoLLM("primary-answer")
    backup = _EchoLLM("backup-answer")
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gateway = cap.gateway()
    response = run(gateway.complete(
        _req("hi"), PortCtx(session_id="s-backend", run_id="r"), degrade_target="backup"
    ), loop)
    assert response.content == "backup-answer"
    assert backup.complete_calls == 1
    assert primary.complete_calls == 0


# =========================================================================== #
# harness.model.gateway（3）
# =========================================================================== #
def test_gateway_registry_property_exact() -> None:
    """输入注入/未注入注册表→registry 属性→精确同源实例或 None→失败判据：注册表未共享。"""
    llm = _EchoLLM("x")
    reg = InMemoryModelRegistry()
    assert ModelGateway(llm=llm, registry=reg).registry is reg
    assert ModelGateway(llm=llm).registry is None


def test_gateway_as_call_uses_typed_target(loop: Any) -> None:
    """输入 typed routing_target=backup/refuse 的 Invocation→as_call→真切后端/真拒答→失败判据：目标未消费或后端回退。"""
    primary = _EchoLLM("primary")
    backup = _EchoLLM("backup")
    reg = InMemoryModelRegistry()
    reg.register_llm("backup", backup)
    gateway = ModelGateway(llm=primary, registry=reg)
    call = gateway.as_call()

    inv = _inv(Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]}))
    inv.write_routing_target(RoutingTarget(target="backup", reason="degraded"))
    result = run(call(inv), loop)
    assert result.ok is True
    assert result.data["content"] == "backup"
    assert backup.complete_calls == 1
    assert primary.complete_calls == 0

    refuse_inv = _inv(Invoke(port="llm", payload={"messages": []}))
    refuse_inv.write_routing_target(RoutingTarget(target="refuse", reason="exhausted"))
    assert run(call(refuse_inv), loop) == Result.failure(
        "REFUSED", "degraded to refusal; no backend invoked"
    )
    assert primary.complete_calls == 0


def test_gateway_stream_yields_chunks_and_settles_cost(loop: Any) -> None:
    """输入流式后端（cost=0.25）→stream→逐块精确产出且账本按 usage 结算→失败判据：块丢失或假零成本。"""
    llm = _EchoLLM("hi", cost=0.25)
    gateway = ModelGateway(llm=llm)
    ctx = PortCtx(session_id="s-stream", run_id="r-stream", meta={"user_id": "u1"})

    async def collect() -> list[str]:
        chunks: list[str] = []
        async for chunk in gateway.stream(_req("go"), ctx):
            chunks.append(chunk)
        return chunks

    assert run(collect(), loop) == ["h", "i"]
    assert llm.stream_calls == 1
    assert gateway.ledger.total() == pytest.approx(0.25)
    assert gateway.ledger.by("user_id", "u1") == pytest.approx(0.25)
