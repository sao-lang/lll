"""R2 组合层承载形态：dispatch 按 port 分派、build_call 函数式装饰器组合、
FaultRecord/RoutingTarget/CallState 具名 DTO，以及经 build_call + make_dispatch
注入 R1 Runner 的真实领域行为（A 类算子 / 装饰器链 / 可靠性执行器端到端）。

全部为领域行为断言（结果值 / 调用次数 / 状态读写），只测新 API——不复活已删除
的抽象拦截语义。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from runtime import (
    Invoke,
    Result,
    Runner,
    Session,
    Terminate,
    Usage,
)

from compose.call import (
    build_call,
    danger_route,
    injection_guard,
    retry_call,
    timeout,
)
from compose.context import Invocation
from compose.dispatch import dispatch, make_dispatch
from compose.guards import CallState, FaultRecord, RoutingTarget
from compose.program import par, seq
from harness.resilience import (
    DegradeLadder,
    DegradeMode,
    Degradation,
    DegradeStep,
    RateLimit,
    ResilienceCapability,
    RetryPolicy,
)


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


async def _base(effect: object, ctx: object) -> Result:
    return Result.success("base-ok")


def _inv(model: str = "primary") -> Invocation:
    """最小 Invocation：共享 meta 字典 + session_id（B1 隔离维度）。"""
    return Invocation(
        effect=Invoke(port="llm", payload={"model": model}),
        ctx=object(),
        session=type("SessionView", (), {"session_id": "r2-call"})(),
        meta={},
    )


def _leaf(name: str, output: object | None = None):
    """叶子 Program：yield 一个带 name 的 Invoke，随后 Terminate。"""
    def program():
        yield Invoke(port="llm", payload={"name": name})
        yield Terminate(output=name if output is None else output)
    return program


def _recorder() -> tuple[object, list[str]]:
    """Port 处理器：记录 name 并回注大写名（真实 Result.data）。"""
    seen: list[str] = []

    def handler(effect: Invoke, ctx: object) -> Result:
        name = str(effect.payload.get("name", ""))
        seen.append(name)
        return Result.success(name.upper(), usage=Usage(prompt_tokens=1))
    return handler, seen


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def test_make_dispatch_routes_by_port():
    """make_dispatch：按 effect.port 找 handler，构造其 Result。"""
    def handler(effect: Invoke, ctx: object) -> Result:
        return Result.success(effect.payload.get("value"))

    call = make_dispatch({"llm": handler})
    result = asyncio.run(call(Invoke(port="llm", payload={"value": 42}), object()))
    assert result.ok and result.data == 42


def test_make_dispatch_missing_port_returns_failure():
    """未注册 Port → PORT_MISSING 失败信封，不抛异常。"""
    result = asyncio.run(make_dispatch({})(Invoke(port="unknown"), object()))
    assert not result.ok and result.error is not None
    assert result.error.code == "PORT_MISSING"
    assert "unknown" in result.error.message


def test_make_dispatch_rejects_non_invoke_effect():
    """非 Invoke（Terminate 等）不进 dispatch → INVALID_EFFECT。"""
    result = asyncio.run(make_dispatch({})(Terminate(output="x"), object()))
    assert result.error is not None and result.error.code == "INVALID_EFFECT"


def test_make_dispatch_folds_handler_exception():
    """Port handler 抛异常 → 折叠为 PORT_CRASHED，含异常类型与消息。"""
    def boom(effect: Invoke, ctx: object) -> Result:
        raise ValueError("bad payload")

    result = asyncio.run(make_dispatch({"llm": boom})(Invoke(port="llm"), object()))
    assert result.error is not None and result.error.code == "PORT_CRASHED"
    assert "ValueError" in result.error.message and "bad payload" in result.error.message


def test_dispatch_convenience_function():
    """dispatch(ports, effect, ctx) 便捷入口与 make_dispatch 语义一致。"""
    result = asyncio.run(dispatch(
        {"llm": lambda e, c: Result.success("direct")},
        Invoke(port="llm"), object(),
    ))
    assert result.ok and result.data == "direct"


# --------------------------------------------------------------------------- #
# call 函数式装饰器 + build_call
# --------------------------------------------------------------------------- #


def test_build_call_wraps_decorators_in_order():
    """build_call：装饰器内层先注册（后注册者在外，最先执行）。"""
    order: list[str] = []

    def mark(label: str):
        def apply(base):
            async def call(effect: object, ctx: object) -> Result:
                order.append(label)
                return await base(effect, ctx)
            return call
        return apply

    async def base(effect: object, ctx: object) -> Result:
        order.append("base")
        return Result.success("ok")

    call = build_call(base, decorators=(mark("d1"), mark("d2")))
    result = asyncio.run(call(Invoke(port="llm"), object()))
    assert result.ok and result.data == "ok"
    assert order == ["d2", "d1", "base"]


def test_build_call_without_decorators_returns_base():
    """build_call：无装饰器时原样返回 base 的 Result（装配恒等）。"""
    call = build_call(_base)
    result = asyncio.run(call(Invoke(port="llm"), object()))
    assert result.ok and result.data == "base-ok"


def test_retry_call_uses_custom_retry_on_and_exhausts():
    """retry_call：retry_on 自定义判定；耗尽后返回末次失败 Result。"""
    attempts = {"n": 0}

    async def flaky(effect: object, ctx: object) -> Result:
        attempts["n"] += 1
        return Result.failure("E", f"attempt-{attempts['n']}")

    call = retry_call(flaky, attempts=3, retry_on=lambda r: "attempt-3" not in r.error.message)
    result = asyncio.run(call(Invoke(port="llm"), object()))
    assert attempts["n"] == 3
    assert not result.ok and result.error is not None
    assert result.error.message == "attempt-3"


def test_retry_call_stops_on_first_success():
    """retry_call：首调成功即返回，不再重试（attempts 为上限而非定数）。"""
    attempts = {"n": 0}

    async def ok(effect: object, ctx: object) -> Result:
        attempts["n"] += 1
        return Result.success("ok")

    result = asyncio.run(retry_call(ok, attempts=5)(Invoke(port="llm"), object()))
    assert result.ok and result.data == "ok"
    assert attempts["n"] == 1


def test_timeout_returns_timeout_failure_on_slow_call():
    """timeout：超过 seconds → TIMEOUT 失败信封（不抛穿）。"""
    async def slow(effect: object, ctx: object) -> Result:
        await asyncio.sleep(0.05)
        return Result.success("too late")

    result = asyncio.run(
        timeout(slow, seconds=0.001)(Invoke(port="llm"), object())
    )
    assert result.error is not None and result.error.code == "TIMEOUT"
    assert "0.001" in result.error.message


def test_timeout_passes_fast_call_through():
    """timeout：未超时时原样返回 core 的 Result。"""
    result = asyncio.run(
        timeout(_base, seconds=1.0)(Invoke(port="llm"), object())
    )
    assert result.ok and result.data == "base-ok"


def test_danger_route_records_danger_into_ctx_meta():
    """danger_route：带 resolve 的 registry 在场时，把 payload.danger 写入 ctx.meta。"""
    meta: dict[str, object] = {}
    ctx = type("Ctx", (), {"meta": meta})()
    seen: list[object] = []

    async def base(effect: object, ctx: object) -> Result:
        seen.append(effect)
        return Result.success("routed")

    class Registry:
        def resolve(self, name: str) -> object:
            return f"sandbox:{name}"

    result = asyncio.run(danger_route(base, Registry())(
        Invoke(port="tool", payload={"danger": "HIGH"}), ctx
    ))
    assert result.ok and len(seen) == 1
    assert meta.get("x-compose.danger_route") == "HIGH"


def test_danger_route_noop_without_resolver_or_non_invoke():
    """danger_route：无 resolve 或非 Invoke Effect 时不写 meta，仍放行 base。"""
    meta: dict[str, object] = {}
    ctx = type("Ctx", (), {"meta": meta})()
    calls: list[str] = []

    async def base(effect: object, ctx: object) -> Result:
        calls.append("base")
        return Result.success("ok")

    asyncio.run(danger_route(base, object())(
        Invoke(port="tool", payload={"danger": "HIGH"}), ctx
    ))
    asyncio.run(danger_route(base, object())(Terminate(output="x"), ctx))
    assert calls == ["base", "base"]
    assert meta == {}


def test_injection_guard_blocks_on_detector_verdict():
    """injection_guard：detector 判 allowed=False → INJECTION_DETECTED，base 零调用。"""
    calls: list[str] = []

    async def base(effect: object, ctx: object) -> Result:
        calls.append("base")
        return Result.success("ok")

    class Detector:
        def detect(self, payload: object, ctx: object) -> object:
            return type("V", (), {"allowed": False})()

    result = asyncio.run(injection_guard(base, Detector())(
        Invoke(port="llm", payload={"text": "<script>"}), object()
    ))
    assert result.error is not None and result.error.code == "INJECTION_DETECTED"
    assert calls == []


def test_injection_guard_allows_when_detector_passes_or_absent():
    """injection_guard：允许裁决 / 无 detect 能力 / 非 Invoke 均放行 base。"""
    calls: list[str] = []

    async def base(effect: object, ctx: object) -> Result:
        calls.append("base")
        return Result.success("ok")

    class Allow:
        def detect(self, payload: object, ctx: object) -> object:
            return type("V", (), {"allowed": True})()

    r1 = asyncio.run(injection_guard(base, Allow())(Invoke(port="llm"), object()))
    r2 = asyncio.run(injection_guard(base, object())(Invoke(port="llm"), object()))
    r3 = asyncio.run(injection_guard(base, Allow())(Terminate(output="x"), object()))
    assert r1.ok and r2.ok and r3.ok
    assert calls == ["base", "base", "base"]


# --------------------------------------------------------------------------- #
# 具名 DTO / CallState
# --------------------------------------------------------------------------- #


def test_fault_and_target_dtos_are_frozen_records():
    """FaultRecord / RoutingTarget：具名 DTO，字段可读且不可变。"""
    fault = FaultRecord(reason="retry_exhausted", failed_target="primary")
    target = RoutingTarget(target="backup", reason="retry_exhausted")
    assert fault.reason == "retry_exhausted" and fault.failed_target == "primary"
    assert target.target == "backup" and target.reason == "retry_exhausted"
    try:
        fault.reason = "mutated"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("FaultRecord 应为 frozen")


def test_call_state_reads_writes_and_filters_wrong_types():
    """CallState：fault/routing_target 读写同源；类型不符按 None 处理。"""
    meta: dict[str, object] = {}
    ctx = type("Ctx", (), {"meta": meta, "session_id": "s2"})()
    state = CallState(ctx)

    assert state.fault() is None and state.routing_target() is None
    state.write_fault(FaultRecord(reason="rate_limited", failed_target="primary"))
    state.write_routing_target(RoutingTarget(target="backup", reason="rate_limited"))
    assert state.fault() == FaultRecord(reason="rate_limited", failed_target="primary")
    assert state.routing_target() == RoutingTarget(target="backup", reason="rate_limited")
    assert state.session_id() == "s2"

    meta["fault"] = "wrong-type"
    meta["routing_target"] = 123
    assert state.fault() is None and state.routing_target() is None


def test_call_state_attaches_meta_when_missing():
    """CallState：ctx 无 meta 时挂回新字典，保证读写同源。"""
    ctx = type("Ctx", (), {"session_id": "s3"})()
    state = CallState(ctx)
    state.write_fault(FaultRecord(reason="timeout"))
    assert isinstance(getattr(ctx, "meta"), dict)
    assert state.fault() == FaultRecord(reason="timeout")


def test_call_state_frozen_ctx_does_not_raise():
    """CallState：ctx 不可写时只做本地读写，不抛穿（异常隔离路径）。"""
    @dataclass(frozen=True)
    class FrozenCtx:
        session_id: str = "frozen"

    state = CallState(FrozenCtx())
    state.write_fault(FaultRecord(reason="breaker_open"))
    assert state.fault() is None  # 无持久 meta，读回 None 是预期行为
    assert state.session_id() == "frozen"


# --------------------------------------------------------------------------- #
# 真实领域行为：A 类算子 / 装饰器链 / ReliabilityExecutor 经 R1 Runner 端到端
# --------------------------------------------------------------------------- #


def test_seq_through_build_call_dispatch_in_runner():
    """seq：真实 Runner 经 build_call(make_dispatch) 按序执行两个 Invoke，输出取末。"""
    handler, seen = _recorder()
    call = build_call(make_dispatch({"llm": handler}))  # type: ignore[arg-type]
    runner = Runner(call=call)
    result = asyncio.run(runner.run(Session(
        session_id="r2-call-seq", program_factory=seq(_leaf("a"), _leaf("b")),
    )))
    assert seen == ["a", "b"]
    assert result.status == "done"
    assert result.output == "b"


def test_par_through_build_call_dispatch_fans_out():
    """par：两个子 Program 的 Invoke 汇成并行批真执行，按槽位聚合输出。"""
    handler, seen = _recorder()
    call = build_call(make_dispatch({"llm": handler}))  # type: ignore[arg-type]
    runner = Runner(call=call)
    result = asyncio.run(runner.run(Session(
        session_id="r2-call-par",
        program_factory=par([_leaf("x"), _leaf("y")], join="all"),
    )))
    assert sorted(seen) == ["x", "y"]
    assert result.output == ["x", "y"]


def test_functional_decorator_chain_runs_in_real_runner():
    """build_call 函数式链（retry + timeout）注入 Runner：重试后成功、产出正确。"""
    attempts = {"n": 0}

    def handler(effect: Invoke, ctx: object) -> Result:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return Result.failure("500", "transient", retryable=True)
        return Result.success(
            {"content": "ok-after-retry", "tool_calls": []},
            usage=Usage(prompt_tokens=1),
        )

    def program():
        resp = yield Invoke(port="llm", payload={"messages": []})
        yield Terminate(output=resp.get("content") if isinstance(resp, dict) else "?")

    call = build_call(
        make_dispatch({"llm": handler}),  # type: ignore[arg-type]
        decorators=[
            lambda base: retry_call(base, attempts=4, backoff=0),
            lambda base: timeout(base, seconds=1.0),
        ],
    )
    runner = Runner(call=call)
    result = asyncio.run(runner.run(Session(
        session_id="r2-call-chain", program_factory=program,
    )))
    assert attempts["n"] == 3
    assert result.output == "ok-after-retry"


def test_executor_degradation_writes_dtos_and_reroutes():
    """ReliabilityExecutor：限流降级写 fault → 选级 → RoutingTarget → core 切 backup。"""
    calls: list[str] = []

    async def core(inv: Invocation) -> Result:
        target = inv.routing_target()
        name = target.target if target else "primary"
        calls.append(name)
        return Result.success(name)

    limit = RateLimit(rate=0.0, burst=0, on_exceed="degrade", clock=lambda: 0.0)
    degradation = Degradation(_ladder(), available={"primary", "backup"})
    executor = ResilienceCapability(
        rate_limit=limit, degradation=degradation
    ).install(core)
    inv = _inv()
    result = asyncio.run(executor.run_invocation(inv))
    assert result.ok and result.data == "backup"
    assert calls == ["backup"]
    assert inv.routing_target() == RoutingTarget(target="backup", reason="rate_limited")
    assert inv.fault() is None  # fault 已被消费，避免重路由重复裁决


def test_executor_refuses_when_ladder_exhausted():
    """ReliabilityExecutor：重试耗尽 → 阶梯全剔除 → REFUSE 真拒答，零静默回退。"""
    calls: list[str] = []

    async def core(inv: Invocation) -> Result:
        target = inv.routing_target()
        name = target.target if target else "primary"
        calls.append(name)
        return Result.failure("500", f"{name} down", retryable=True)

    retry = RetryPolicy(max_attempts=1, base_delay=0.001, jitter="none")
    degradation = Degradation(_ladder(), available={"primary", "backup"})
    executor = ResilienceCapability(retry=retry, degradation=degradation).install(core)
    result = asyncio.run(executor.run_invocation(_inv()))
    assert not result.ok and result.error is not None and result.error.code == "REFUSED"
    assert calls.count("primary") == 1 and calls.count("backup") == 1


def test_call_state_and_executor_share_same_meta():
    """CallState 与 ReliabilityExecutor 共享同一份 meta：DTO 读写同源可见。"""
    inv = _inv()
    state = CallState(inv)
    state.write_fault(FaultRecord(reason="rate_limited", failed_target="primary"))

    # 执行器复用 Invocation.meta（_as_invocation 同源），故预先写入的 fault 可见。
    seen: list[str] = []

    async def core(i: Invocation) -> Result:
        seen.append("core")
        return Result.success("done")

    executor = ResilienceCapability().install(core)
    asyncio.run(executor.run_invocation(inv))
    assert seen == ["core"]
    assert state.fault() == FaultRecord(reason="rate_limited", failed_target="primary")
