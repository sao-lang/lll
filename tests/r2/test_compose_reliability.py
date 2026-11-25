"""R2 组合层可靠性端到端：A 类算子经 build_call+make_dispatch 注入 R1 Runner 跑通，
以及 ResilienceCapability 产出的 ReliabilityExecutor 用领域策略跑通真实调用。

覆盖 D-3 算子核心（seq/par/race/map_effect/fallback/retry + loop_until/branch_value）
与 ToolLayer 外壳；断言真实 Effect 序列 / 聚合输出 / 调用次数，不放水。
原 branch/loop/map_state/gate 依赖不存在的 port="state"，真实 Runner 下静默误路由，已删除。
不触碰已删除的五角色语义——只装配领域策略与函数式 Program 变换。
"""
from __future__ import annotations

import asyncio

from runtime import (
    AgentState,
    Invoke,
    Program,
    Result,
    Runner,
    Session,
    Terminate,
    Usage,
)

from compose import Invocation, build_call, make_dispatch
from compose.program import (
    branch_value,
    fallback,
    loop_until,
    map_effect,
    par,
    race,
    retry,
    seq,
)
from compose.tool_layer import layer_program
from harness.resilience import (
    DegradeLadder,
    DegradeMode,
    Degradation,
    DegradeStep,
    Idempotency,
    KillSwitch,
    RateLimit,
    ResilienceCapability,
    RetryPolicy,
)

_PortHandler = object  # make_dispatch 的 PortMap 值为 (Invoke, object) -> Result 的可调用对象


def _leaf(
    name: str,
    output: object = None,
    *,
    fail: bool | None = None,
):
    """叶子 Program：yield 一个带 name 的 Invoke，随后 Terminate。

    fail 显式写 scratch 的 x-compose.fail（fallback/retry 读取的失败标记），
    这是 A 类算子约定的横切数据通道，不是角色方法。
    """
    def program():
        payload: dict[str, object] = {"name": name}
        if fail is not None:
            payload["x-compose.fail"] = fail
        yield Invoke(port="llm").with_payload(**payload)
        yield Terminate(output=name if output is None else output)
    return program


def _terminate_only(output: object):
    """无 Effect、直接 Terminate 的 Program（用于 par 的提前完成分支）。"""
    def program():
        yield Terminate(output=output)
    return program


def _recorder():
    """Port 处理器：记录 name 并回注大写名（真实 Result.data）。"""
    seen: list[str] = []

    def handler(effect: Invoke, ctx: object) -> Result:
        name = str(effect.payload.get("name", ""))
        seen.append(name)
        return Result.success(name.upper(), usage=Usage(prompt_tokens=1))
    return handler, seen


def _run(
    program,
    *,
    handlers: dict[str, object] | None = None,
    state: AgentState | None = None,
    session_id: str = "r2-compose",
):
    """用真实 R1 Runner 跑组合 Program：build_call(make_dispatch(ports)) 注入。"""
    ports: dict[str, object] = {
        # A 类算子用 port="state" 的 Invoke 引导回注初始 State（组合层内部探针）。
        "state": lambda effect, ctx: Result.success(
            state if state is not None else AgentState()
        )
    }
    if handlers:
        ports.update(handlers)
    call = build_call(make_dispatch(ports))  # type: ignore[arg-type]
    runner = Runner(call=call)
    return asyncio.run(
        runner.run(Session(session_id=session_id, program_factory=program))
    )


# --------------------------------------------------------------------------- #
# A 类算子：真实 Runner 编排
# --------------------------------------------------------------------------- #


def test_seq_runs_in_order_and_propagates_second_output():
    """seq：先 a 后 b，真实 Runner 按序执行两个 Invoke，整体输出取 b。"""
    handler, seen = _recorder()
    result = _run(seq(_leaf("a"), _leaf("b")), handlers={"llm": handler})
    assert seen == ["a", "b"]
    assert result.status == "done"
    assert result.output == "b"


def test_par_all_join_fans_out_and_aggregates_in_order():
    """par(join=all)：两个子 Program 的 Invoke 汇成并行批真执行，输出按槽位聚合。"""
    handler, seen = _recorder()
    result = _run(
        par([_leaf("x"), _leaf("y")], join="all"), handlers={"llm": handler}
    )
    assert sorted(seen) == ["x", "y"]
    assert result.output == ["x", "y"]


def test_par_any_join_picks_first_non_none():
    """par(join=any)：提前 Terminate 的子槽位为 None，any 取首个非 None 输出。"""
    result = _run(
        par([_terminate_only(None), _leaf("y")], join="any"),
        handlers={"llm": _recorder()[0]},
    )
    assert result.output == "y"


def test_par_majority_join_counts_votes_deterministically():
    """par(join=majority)：多数票胜出，平票取首现（确定性）。"""
    result = _run(
        par([_leaf("a"), _leaf("a"), _leaf("b")], join="majority"),
        handlers={"llm": _recorder()[0]},
    )
    assert result.output == "a"


def test_par_majority_hashable_fallback_for_unhashable_outputs():
    """par(join=majority)：dict 输出不可哈希，归一为字符串后仍能多数决。"""
    result = _run(
        par(
            [_leaf("a", {"v": 1}), _leaf("b", {"v": 1}), _leaf("c", {"v": 2})],
            join="majority",
        ),
        handlers={"llm": _recorder()[0]},
    )
    assert result.output == {"v": 1}


def test_branch_value_selects_arm_from_source_output():
    """branch_value：从 source 的 Terminate.output 取值选 arm，真实 Runner 只走命中臂。"""
    handler, seen = _recorder()

    def source():
        yield Invoke(port="llm", payload={"name": "router"})
        yield Terminate(output={"pick": True})

    result = _run(
        branch_value(
            source,
            lambda value: "yes" if value["pick"] else "no",
            {"yes": _leaf("yes"), "no": _leaf("no")},
        ),
        handlers={"llm": handler},
    )
    assert seen == ["router", "yes"]
    assert result.output == "yes"


def test_branch_value_no_route_terminates_with_reason():
    """branch_value：key 缺失且无 default → Terminate(no_route)，不执行任何 arm。"""
    handler, seen = _recorder()

    def source():
        yield Invoke(port="llm", payload={"name": "router"})
        yield Terminate(output={"pick": "missing"})

    result = _run(
        branch_value(source, lambda value: str(value["pick"]), {"yes": _leaf("yes")}),
        handlers={"llm": handler},
    )
    assert seen == ["router"]
    assert result.reason == "no_route"
    assert result.output is None


def test_race_returns_first_completed_output():
    """race：并行推进各子 Program，首个 Terminate 的输出胜出。"""
    handler, seen = _recorder()
    result = _run(
        race([_leaf("r1"), _leaf("r2")]), handlers={"llm": handler}
    )
    assert sorted(seen) == ["r1", "r2"]
    assert result.output == "r1"


def test_loop_until_repeats_until_done():
    """loop_until：按回注值驱动，done 命中前 body 恰好执行两次并输出末轮值。"""
    handler, seen = _recorder()
    marks = {"n": 0}

    def step(feedback: object):
        def program_step():
            marks["n"] += 1
            yield Invoke(port="llm", payload={"name": f"body{marks['n']}"})
            yield Terminate(output=marks["n"])
        return program_step

    result = _run(
        loop_until(step, done=lambda out: int(out) >= 2, max_rounds=5),
        handlers={"llm": handler},
    )
    assert seen == ["body1", "body2"]
    assert result.output == 2


def test_map_effect_rewrites_payload_and_passes_terminate():
    """map_effect：非终态 Effect 被改写（脱敏标记），Terminate 原样透传。"""
    captured: list[Invoke] = []

    def handler(effect: Invoke, ctx: object) -> Result:
        captured.append(effect)
        return Result.success("ok")

    def scrub(effect):
        if isinstance(effect, Invoke):
            return effect.with_payload(redacted=True)
        return effect

    result = _run(map_effect(scrub)(_leaf("secret")), handlers={"llm": handler})
    assert result.output == "secret"
    assert len(captured) == 1
    assert captured[0].payload.get("redacted") is True


def test_fallback_switches_to_ladder_on_failure():
    """fallback：首选 arm 写失败标记 → 切 ladder；成功 arm 清标记后产出其 output。"""
    handler, seen = _recorder()
    result = _run(
        fallback(
            _leaf("primary", fail=True),
            ladders=[_leaf("backup", fail=False)],
            refuse=_leaf("refuse"),
        ),
        handlers={"llm": handler},
    )
    assert seen == ["primary", "backup"]
    assert result.output == "backup"


def test_fallback_all_failed_terminates_with_reason():
    """fallback：所有 arm（含 refuse）都失败 → Terminate(all_failed)。"""
    handler, seen = _recorder()
    result = _run(
        fallback(
            _leaf("p", fail=True),
            ladders=[_leaf("l", fail=True)],
            refuse=_leaf("r", fail=True),
        ),
        handlers={"llm": handler},
    )
    assert seen == ["p", "l", "r"]
    assert result.reason == "all_failed"
    assert result.output is None


def test_retry_replays_until_success():
    """retry（流程级）：失败按 program_factory 重建重跑，成功即停。"""
    handler, seen = _recorder()
    marks = {"n": 0}

    def flaky():
        marks["n"] += 1
        return _leaf("r", fail=marks["n"] < 2)()  # ProgramFn 须产出 generator

    result = _run(retry(flaky, attempts=3), handlers={"llm": handler})
    assert seen == ["r", "r"]
    assert marks["n"] == 2
    assert result.output == "r"


def test_retry_stops_at_attempt_cap():
    """retry：持续失败时按 attempts 上限停止，输出末次结果。"""
    handler, seen = _recorder()
    marks = {"n": 0}

    def always_fail():
        marks["n"] += 1
        return _leaf("r", fail=True)()

    result = _run(retry(always_fail, attempts=2), handlers={"llm": handler})
    assert seen == ["r", "r"]
    assert marks["n"] == 2
    assert result.output == "r"


def test_layer_program_runs_as_port_shell():
    """ToolLayer 外壳：layer_program 构造的 Program 经 dispatch 走真实 Port。"""
    def handler(effect: Invoke, ctx: object) -> Result:
        assert effect.payload.get("name") == "layer"
        return Result.success("backend-data")

    result = _run(
        layer_program("llm", payload={"name": "layer"}, output="L"),
        handlers={"llm": handler},
    )
    assert result.output == "L"


# --------------------------------------------------------------------------- #
# ReliabilityExecutor：领域策略 + 函数式变换器（非角色）
# --------------------------------------------------------------------------- #


def _ladder() -> DegradeLadder:
    return DegradeLadder(steps=(
        DegradeStep(target="primary", mode=DegradeMode.REROUTE, quality=1.0),
        DegradeStep(target="backup", mode=DegradeMode.REROUTE, quality=0.6),
        DegradeStep(target="", mode=DegradeMode.REFUSE, quality=0.0),
    ))


def _inv(model: str = "primary", *, idem: str | None = None) -> Invocation:
    effect = Invoke(port="llm", payload={"model": model})
    if idem is not None:
        effect = Invoke(
            port="llm", payload={"model": model}, idempotency_key=idem
        )
    return Invocation(
        effect=effect,
        ctx=object(),
        session=type("SessionView", (), {"session_id": "r2-rel"})(),
        meta={},
    )


def test_executor_as_call_runs_through_runner():
    """ReliabilityExecutor.as_call() 注入 Runner：真实链路上 core 恰好调用一次。"""
    calls: list[str] = []

    async def core(inv: Invocation) -> Result:
        calls.append("core")
        return Result.success({"content": "ok", "tool_calls": []})

    def program():
        resp = yield Invoke(port="llm", payload={"messages": []})
        yield Terminate(output=resp.get("content") if isinstance(resp, dict) else "?")

    executor = ResilienceCapability().wrap(core)
    runner = Runner(call=executor.as_call())
    result = asyncio.run(
        runner.run(Session(session_id="r2-exec", program_factory=program))
    )
    assert calls == ["core"]
    assert result.output == "ok"


def test_executor_killswitch_rejects_before_core():
    """KillSwitch 置位 → INVOKE_REJECTED，core 零调用，notify 收到具名原因。"""
    calls: list[str] = []
    notices: list[str] = []

    async def core(inv: Invocation) -> Result:
        calls.append("core")
        return Result.success("never")

    ks = KillSwitch(flag_store={"kill:global": True})
    executor = ResilienceCapability(killswitch=ks).wrap(
        core, notify=lambda phase, payload: notices.append(phase)
    )
    result = asyncio.run(executor.run_invocation(_inv()))
    assert result.error is not None and result.error.code == "INVOKE_REJECTED"
    assert calls == []
    assert notices == ["invoke_rejected"]


def test_executor_idempotency_blocks_duplicate_side_effect():
    """Idempotency：同键第二次调用被去重拦截，core 仅调用一次。"""
    calls: list[str] = []

    async def core(inv: Invocation) -> Result:
        calls.append("core")
        return Result.success("ok")

    executor = ResilienceCapability(
        idempotency=Idempotency(store={}, clock=lambda: 0.0)
    ).wrap(core)
    first = asyncio.run(executor.run_invocation(_inv(idem="K-R2")))
    second = asyncio.run(executor.run_invocation(_inv(idem="K-R2")))
    assert first.ok and first.data == "ok"
    assert second.error is not None and second.error.code == "INVOKE_REJECTED"
    assert calls == ["core"]


def test_executor_rate_limit_degrade_reroutes_to_backup():
    """限流 DEGRADE → 写具名 fault → Degradation.select → typed target 切 backup。"""
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
    ).wrap(core)
    inv = _inv()
    result = asyncio.run(executor.run_invocation(inv))
    assert result.ok and result.data == "backup"
    target = inv.routing_target()
    assert target is not None and target.target == "backup"
    assert target.reason == "rate_limited"
    assert calls == ["backup"]


def test_executor_retry_exhaustion_reduces_to_backup():
    """真实 RetryPolicy 耗尽 → fault 带 failed_target → 降级选 backup；次数精确。"""
    calls: list[str] = []

    async def core(inv: Invocation) -> Result:
        target = inv.routing_target()
        name = target.target if target else "primary"
        calls.append(name)
        if name == "primary":
            return Result.failure("500", "primary down", retryable=True)
        return Result.success(name)

    retry_policy = RetryPolicy(max_attempts=2, base_delay=0.001, jitter="none")
    executor = ResilienceCapability(
        retry=retry_policy, degradation=Degradation(_ladder(), available={"primary", "backup"})
    ).wrap(core)
    result = asyncio.run(executor.run_invocation(_inv()))
    assert result.ok and result.data == "backup"
    assert calls.count("primary") == 2
    assert calls.count("backup") == 1


def test_executor_functional_transformer_extends_core():
    """extra_plugins 只收函数式 (CallFn)->CallFn 变换器，按装饰顺序包裹 core。"""
    order: list[str] = []

    def tag(label: str):
        def transform(next_call):
            async def wrapped(inv: Invocation) -> Result:
                order.append(label)
                return await next_call(inv)
            return wrapped
        return transform

    async def core(inv: Invocation) -> Result:
        order.append("core")
        return Result.success("ok")

    cap = ResilienceCapability(extra_plugins=(tag("t1"), tag("t2")))
    assert len(cap.plugins()) == 2  # 仅函数式变换器，无角色实例
    executor = cap.wrap(core)
    result = asyncio.run(executor.run_invocation(_inv()))
    assert result.ok and result.data == "ok"
    assert order == ["t2", "t1", "core"]
