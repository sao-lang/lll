"""补齐「当前 e2e 从未执行过」的公开 API：compose.* / harness.context.* / harness.memory.*
/ harness.rag.* / harness.tools.* / ports.adapters.mcp|provider。

纪律：每个用例走真入口（CallFn、Agent、Program、ToolFn、真实 JSON-RPC 帧等），
用精确返回值与可观察副作用断言；不用 isinstance / hasattr / registry.has 当证据、
不用 is-not-None / callable / 恒真式。异步统一用 conftest 的 loop + run；
@tool 全局 pending 用模块 autouse fixture 隔离；每用例独立实例。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from compose import (
    DangerLevel,
    FaultRecord,
    HookBusError,
    LifecycleBus,
    LifecyclePhase,
    Notice,
    PortCtx,
    Registry,
    RoutingTarget,
    ToolCall,
    ToolSpec,
)
from compose.context import CallContext
from compose.guards import CallState
from harness.rag.types import Chunk, Hit, Query, RAGResult, TokenBudget
from runtime import AgentState, Budgets, Invoke, Message, Result, Terminate

from conftest import run
from tests.r12.conftest import drive


def _drive_program(program: Any) -> tuple[list[Any], Any]:
    """驱动已实例化的 Program（drive 需要工厂；build()/layer_program() 直接给 generator）。"""
    return drive(lambda: program)


@pytest.fixture(autouse=True)
def _pending_tool_isolation() -> Any:
    """@tool 全局待注册容器按用例隔离，避免跨用例污染 pending_tools 快照。"""
    from harness.tools import clear_pending

    clear_pending()
    yield
    clear_pending()


class _Provider:
    """注册表 provider 最小实现（供 Registry.register/resolve/has 真消费）。"""

    def __init__(self, name: str) -> None:
        self.name = name


class _Cap:
    """Capability 协议实现：install 真把 provider 注册进传入的 Registry。"""

    name = "demo_cap"
    version = "1.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.installs: list[object] = []

    def install(self, container: object, reg: Registry) -> None:
        self.installs.append(container)
        reg.register("demo.port", self.provider)


def _term_program(output: Any) -> Any:
    def program():
        yield Terminate(output=output)
        return None

    return program


class _TermArch:
    """静态检查干净、compile 产出 Terminate 的最小 Arch。"""

    name = "coverage-agent"
    version = "1.0.0"

    def __init__(self, output: Any = "from-arch") -> None:
        self.output = output

    def static_check(self) -> Any:
        from archs.base import StaticCheckReport

        return StaticCheckReport(arch=self.name, version=self.version, ports_required=())

    def compile(self) -> Any:
        return _term_program(self.output)()


class _BadArch(_TermArch):
    """静态检查带 error 的 Arch：build 必须在装配期拒绝。"""

    def static_check(self) -> Any:
        from archs.base import StaticCheckIssue, StaticCheckReport

        return StaticCheckReport(
            errors=(StaticCheckIssue(code="BAD_ARCH", message="no ports"),),
            arch=self.name,
            version=self.version,
        )


class _Counter:
    """cache 装饰器计次用：记录 base 被真正调用的次数。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []


# =========================================================================== #
# compose.call（9）：retry_call / timeout / danger_route / injection_guard / build_call
# =========================================================================== #
def test_call01_retry_call_retries_only_when_result_says_so(loop: Any) -> None:
    """输入：base 前两次返回 retryable 失败、第三次成功；retry_on=False 另走一次。
    真实入口：compose.call.retry_call(...) 产出的 CallFn（其内部 call 闭包）。
    精确断言：成功路径 data='done'、尝试次数=[1,1,1]；retry_on 短路时只尝试一次且保留失败码。
    失败判据：base 调用次数不是 3/1，或返回值不是成功/'TEMP' 失败。
    """
    from compose.call import retry_call

    tries: list[int] = []

    async def base(effect: Any, ctx: object) -> Result:
        tries.append(int(effect.payload["n"]))
        if len(tries) < 3:
            return Result.failure("TEMP", "retryable", retryable=True)
        return Result.success("done")

    call = retry_call(base, attempts=5)
    first = run(call(Invoke(port="p").with_payload(n=1), object()), loop)
    assert first.ok is True
    assert first.data == "done"
    assert tries == [1, 1, 1]

    tries.clear()
    call_short = retry_call(base, attempts=5, retry_on=lambda result: False)
    second = run(call_short(Invoke(port="p").with_payload(n=1), object()), loop)
    assert second.ok is False
    assert (second.error.code, second.error.message, second.error.retryable) == (
        "TEMP", "retryable", True,
    )
    assert tries == [1]


def test_call02_timeout_folds_deadline_into_result(loop: Any) -> None:
    """输入：sleep 0.05s 的 base + seconds=0.001；另一个立即返回的 base。
    真实入口：compose.call.timeout(base, seconds=...) 的 call 闭包。
    精确断言：超时 Result.ok=False、error.code='TIMEOUT'、message 含精确秒数；快速路径 ok=True/data='fast'。
    失败判据：sleep 没被打断、错误码或消息不符。
    """
    from compose.call import timeout

    async def slow(effect: Any, ctx: object) -> Result:
        await asyncio.sleep(0.05)
        return Result.success("late")

    async def fast(effect: Any, ctx: object) -> Result:
        return Result.success("fast")

    folded = run(timeout(slow, seconds=0.001)(Invoke(port="p"), object()), loop)
    assert folded.ok is False
    assert folded.error.code == "TIMEOUT"
    assert folded.error.message == "call exceeded 0.001s"

    ok = run(timeout(fast, seconds=1.0)(Invoke(port="p"), object()), loop)
    assert ok.ok is True and ok.data == "fast"


def test_call03_danger_route_writes_named_meta_key(loop: Any) -> None:
    """输入：带 payload.danger='critical' 的 Invoke + 有 resolve 的 registry + ctx.meta={}。
    真实入口：compose.call.danger_route(base, registry) 的 call 闭包。
    精确断言：ctx.meta['x-compose.danger_route']=='critical' 且 base 结果原样透传。
    失败判据：meta 键缺失/值不是 danger 值，或 base 未被调用。
    """
    from compose.call import danger_route

    class _Resolver:
        name = "resolver"

        def resolve(self, name: str) -> str:
            return name

    seen: list[str] = []

    async def base(effect: Any, ctx: object) -> Result:
        seen.append("base")
        return Result.success("routed")

    ctx = PortCtx(session_id="s1", meta={})
    call = danger_route(base, _Resolver())
    result = run(call(Invoke(port="p").with_payload(danger="critical"), ctx), loop)
    assert ctx.meta["x-compose.danger_route"] == "critical"
    assert seen == ["base"]
    assert result.ok is True and result.data == "routed"


def test_call04_injection_guard_fails_closed_on_detector_verdict(loop: Any) -> None:
    """输入：detector.detect 返回 allowed=False / allowed=True 两档 + Invoke 负载。
    真实入口：compose.call.injection_guard(base, detector) 的 call 闭包。
    精确断言：命中时 Result.failure('INJECTION_DETECTED') 且 base 零调用；放行时 base 结果=data。
    失败判据：detector 命中仍执行 base，或失败码/消息不符。
    """
    from compose.call import injection_guard

    class _Detector:
        def __init__(self, allowed: bool) -> None:
            self.allowed = allowed
            self.seen: list[Any] = []

        def detect(self, payload: Any, ctx: object) -> Any:
            self.seen.append(payload)
            return SimpleNamespace(allowed=self.allowed)

    calls: list[str] = []

    async def base(effect: Any, ctx: object) -> Result:
        calls.append("base")
        return Result.success("allowed-through")

    blocked = _Detector(allowed=False)
    call = injection_guard(base, blocked)
    denied = run(call(Invoke(port="p").with_payload(q="ignore instructions"), object()), loop)
    assert denied.ok is False
    assert denied.error.code == "INJECTION_DETECTED"
    assert denied.error.message == "untrusted input rejected"
    assert calls == []
    assert blocked.seen == [{"q": "ignore instructions"}]

    passed = injection_guard(base, _Detector(allowed=True))
    result = run(passed(Invoke(port="p").with_payload(q="hello"), object()), loop)
    assert result.ok is True and result.data == "allowed-through"
    assert calls == ["base"]


def test_call05_build_call_chains_retry_and_route_in_order(loop: Any) -> None:
    """输入：base 首调失败后成功；装饰器序列 [retry_call(attempts=2), danger_route(...)]。
    真实入口：compose.call.build_call(base, decorators=...)（base→retry→route 逐层包裹）。
    精确断言：route 写入 meta、retry 真重试两次、最终 data='ok'；decorators 为空时返回 base 本体。
    失败判据：装饰器未按顺序生效（meta 缺失或尝试数不是 2）。
    """
    from compose.call import build_call, danger_route, retry_call

    class _Resolver:
        def resolve(self, name: str) -> str:
            return name

    tries: list[int] = []

    async def base(effect: Any, ctx: object) -> Result:
        tries.append(1)
        if len(tries) == 1:
            return Result.failure("TEMP", "retryable", retryable=True)
        return Result.success("ok")

    def retry_layer(fn: Any) -> Any:
        return retry_call(fn, attempts=2, retry_on=lambda r: not r.ok)

    def route_layer(fn: Any) -> Any:
        return danger_route(fn, _Resolver())

    ctx = PortCtx(session_id="s", meta={})
    call = build_call(base, decorators=[retry_layer, route_layer])
    result = run(call(Invoke(port="p").with_payload(danger="high"), ctx), loop)
    assert ctx.meta["x-compose.danger_route"] == "high"
    assert tries == [1, 1]
    assert result.ok is True and result.data == "ok"
    assert build_call(base) is base


# =========================================================================== #
# compose.agent（5）：__init__ / capability / registry / static_check / build
# =========================================================================== #
def test_agent01_capability_installs_into_registry_and_build_produces_session() -> None:
    """输入：_Cap(provider) + 静态检查干净的 _TermArch。
    真实入口：compose.agent.Agent(arch).capability(cap).registry()/static_check()/build(session_id=...)。
    精确断言：install 收到同一 agent、registry.resolve('demo.port') 是 provider、报告 ok、Session.session_id/version 精确，工厂驱动出 Terminate.output='from-arch'。
    失败判据：provider 未进 registry、版本串不符或工厂不产出 Terminate。
    """
    from compose.agent import Agent

    provider = _Provider("demo.provider")
    cap = _Cap(provider)
    agent = Agent(_TermArch())
    assert agent.capability(cap) is agent
    assert cap.installs == [agent]
    assert agent.registry().resolve("demo.port") is provider

    report = agent.static_check()
    assert getattr(report, "ok") is True
    assert getattr(report, "arch") == "coverage-agent"

    session = agent.build(session_id="sess-42")
    assert session.session_id == "sess-42"
    assert session.version == "coverage-agent@1.0.0"
    effects, state = drive(session.program_factory)
    assert len(effects) == 1
    assert effects[0].output == "from-arch"
    assert state.output == "from-arch"


def test_agent02_build_rejects_report_with_errors() -> None:
    """输入：static_check 报告含 error code BAD_ARCH 的 Arch。
    真实入口：compose.agent.Agent(bad_arch).build()。
    精确断言：ValueError 消息精确为 'static_check failed: BAD_ARCH'。
    失败判据：错误被放行产出 Session，或消息不含错误码。
    """
    from compose.agent import Agent

    with pytest.raises(ValueError, match="static_check failed: BAD_ARCH"):
        Agent(_BadArch()).build()


# =========================================================================== #
# compose.context（5） + compose.guards（4）
# =========================================================================== #
def test_context01_call_context_writes_fault_and_target_then_exposes_readonly_view() -> None:
    """输入：CallContext(effect=Invoke, ctx, session) + FaultRecord/RoutingTarget。
    真实入口：CallContext.session_id/write_fault/write_routing_target/routing_target/meta_view。
    精确断言：session_id='sess-77'；fault 与 target 原对象可读回；meta_view 快照精确且不可写；后续 meta 变更不污染快照。
    失败判据：写入未落到 meta、快照可写或快照随 meta 变化。
    """
    session = SimpleNamespace(session_id="sess-77")
    cc = CallContext(effect=Invoke(port="llm"), ctx=object(), session=session)
    assert cc.session_id == "sess-77"

    fault = FaultRecord(reason="timeout", failed_target="backup")
    target = RoutingTarget(target="backup", reason="timeout")
    cc.write_fault(fault)
    cc.write_routing_target(target)
    assert cc.fault() is fault
    assert cc.routing_target() is target

    view = cc.meta_view()
    assert dict(view) == {"fault": fault, "routing_target": target}
    with pytest.raises(TypeError):
        view["extra"] = 1  # type: ignore[index]
    cc.meta["extra"] = "later"
    assert "extra" in dict(cc.meta_view())
    assert "extra" not in dict(view)

    assert CallContext(effect=Invoke(port="p"), ctx=object(), session=object()).session_id == ""


def test_guards01_call_state_shares_ctx_meta_and_derives_session_id() -> None:
    """输入：带 meta 与 session_id 的 PortCtx；另取无 meta 的裸对象。
    真实入口：compose.guards.CallState.write_fault/fault/write_routing_target/session_id。
    精确断言：fault/target 写入 ctx.meta 具名键并原样读回；session_id 精确；ctx 无 meta 时挂回新字典。
    失败判据：读写不同源、session_id 丢失或裸对象未获得 meta。
    """
    ctx = PortCtx(session_id="sess-g", meta={})
    state = CallState(ctx)
    fault = FaultRecord(reason="rate_limited", failed_target="secondary")
    target = RoutingTarget(target="secondary", reason="rate_limited")
    state.write_fault(fault)
    state.write_routing_target(target)
    assert state.fault() is fault
    assert state.routing_target() is target
    assert state.session_id() == "sess-g"
    assert ctx.meta["fault"] is fault
    assert ctx.meta["routing_target"] is target

    bare = SimpleNamespace(session_id="")
    bare_state = CallState(bare)
    bare_state.write_fault(fault)
    assert bare.meta["fault"] is fault
    assert bare_state.session_id() == ""


# =========================================================================== #
# compose.dispatch（3）
# =========================================================================== #
def test_dispatch01_make_dispatch_routes_invoke_and_folds_all_failures(loop: Any) -> None:
    """输入：ports={'echo':成功 handler, 'boom':抛异常 handler} + Invoke/Terminate 两类 Effect。
    真实入口：compose.dispatch.make_dispatch(ports) 的 call 闭包。
    精确断言：成功 data 精确；缺端口 PORT_MISSING/port 名；非 Invoke INVALID_EFFECT；异常 PORT_CRASHED 且消息精确。
    失败判据：异常抛穿、错误码不符或 handler 未按端口分派。
    """
    from compose.dispatch import make_dispatch

    def echo(effect: Any, ctx: object) -> Result:
        return Result.success(effect.payload["v"])

    def boom(effect: Any, ctx: object) -> Result:
        raise RuntimeError("kaboom")

    call = make_dispatch({"echo": echo, "boom": boom})
    ok = run(call(Invoke(port="echo").with_payload(v=17), object()), loop)
    assert ok.ok is True and ok.data == 17

    missing = run(call(Invoke(port="absent"), object()), loop)
    assert missing.ok is False
    assert missing.error.code == "PORT_MISSING"
    assert missing.error.message == "port not registered: absent"

    invalid = run(call(Terminate(output="x"), object()), loop)
    assert invalid.ok is False
    assert invalid.error.code == "INVALID_EFFECT"
    assert invalid.error.message == "dispatch only handles Invoke"

    crashed = run(call(Invoke(port="boom"), object()), loop)
    assert crashed.ok is False
    assert crashed.error.code == "PORT_CRASHED"
    assert crashed.error.message == "RuntimeError: kaboom"


def test_dispatch02_dispatch_convenience_entry_delegates_to_make_dispatch(loop: Any) -> None:
    """输入：ports={'add':求和 handler} + Invoke(add, a=2,b=3)。
    真实入口：compose.dispatch.dispatch(ports, effect, ctx) 异步便捷入口。
    精确断言：Result.ok=True、data=5；未知 port 返回 PORT_MISSING。
    失败判据：便捷入口结果与 make_dispatch 分派结果不一致。
    """
    from compose.dispatch import dispatch

    ports = {"add": lambda eff, ctx: Result.success(eff.payload["a"] + eff.payload["b"])}
    result = run(dispatch(ports, Invoke(port="add").with_payload(a=2, b=3), object()), loop)
    assert result.ok is True and result.data == 5
    missing = run(dispatch(ports, Invoke(port="none"), object()), loop)
    assert missing.ok is False
    assert missing.error.code == "PORT_MISSING"


# =========================================================================== #
# compose.notify（2）
# =========================================================================== #
def test_notify01_notice_from_invocation_projects_readonly_fields() -> None:
    """输入：CallContext(session_id='s1', effect=Invoke(port='llm'), meta={'k':'v'}) + 专属 payload。
    真实入口：compose.notify.Notice.from_invocation(phase, inv, payload=...)。
    精确断言：phase/session_id/port/payload/meta 精确；payload 与 meta 均为只读映射。
    失败判据：上下文投影字段错位或映射可写。
    """
    inv = CallContext(
        effect=Invoke(port="llm"),
        ctx=object(),
        session=SimpleNamespace(session_id="s1"),
        meta={"k": "v"},
    )
    notice = Notice.from_invocation(
        LifecyclePhase.BEFORE_MODEL, inv, payload={"exceeded": True}
    )
    assert notice.phase is LifecyclePhase.BEFORE_MODEL
    assert notice.session_id == "s1" and notice.port == "llm"
    assert dict(notice.payload) == {"exceeded": True}
    assert dict(notice.meta) == {"k": "v"}
    with pytest.raises(TypeError):
        notice.meta["k2"] = "v2"  # type: ignore[index]


def test_notify02_bus_subscribe_plugin_registers_real_subscriber() -> None:
    """输入：实现 name+register(bus) 的插件；另一缺 register 的坏插件。
    真实入口：compose.notify.LifecycleBus.subscribe(plugin) + emit(notice)。
    精确断言：subscribe 返回同一 bus；emit 后插件记录到精确 notice；坏插件 ValueError 消息精确。
    失败判据：插件未订阅成功，或坏插件被静默接受。
    """
    bus = LifecycleBus()

    class _Plugin:
        name = "collector"

        def __init__(self) -> None:
            self.seen: list[Notice] = []

        def register(self, target: LifecycleBus) -> None:
            target.on(LifecyclePhase.BEFORE_TOOL, self.seen.append, name="collect")

    plugin = _Plugin()
    assert bus.subscribe(plugin) is bus
    notice = Notice(phase=LifecyclePhase.BEFORE_TOOL, session_id="sess-n")
    bus.emit(notice)
    assert len(plugin.seen) == 1
    assert plugin.seen[0].session_id == "sess-n"
    assert plugin.seen[0].phase is LifecyclePhase.BEFORE_TOOL

    with pytest.raises(ValueError, match="lifecycle plugin must have name and register\\(bus\\)"):
        bus.subscribe(SimpleNamespace(name="bad"))


# =========================================================================== #
# compose.registry（2）
# =========================================================================== #
def test_registry01_has_and_freeze_change_write_behavior() -> None:
    """输入：空 Registry + provider('p')。
    真实入口：compose.registry.Registry.has / freeze（配合 register 观察冻结效果）。
    精确断言：has('p') 先 False 后 True；freeze 返回 self；冻结后 register 抛 HookBusError('registry is frozen')。
    失败判据：has 值不符或冻结后仍能写入。
    """
    reg = Registry()
    provider = _Provider("p")
    assert reg.has("p") is False
    assert reg.register("p", provider) is reg
    assert reg.has("p") is True
    assert reg.has("absent") is False
    assert reg.freeze() is reg
    with pytest.raises(HookBusError, match="registry is frozen"):
        reg.register("q", _Provider("q"))


# =========================================================================== #
# compose.tool_layer（2） + compose.program（3）
# =========================================================================== #
def test_tool_layer01_layer_program_yields_invoke_then_terminate() -> None:
    """输入：layer_program('memory', payload={'layer':'context'}, output='DONE')()。
    真实入口：compose.tool_layer.layer_program 返回的 program 闭包。
    精确断言：Effect 序列恰两条——Invoke(port='memory', payload={'layer':'context'}) 与 Terminate(output='DONE')。
    失败判据：端口/负载/终态输出任一不符，或产出额外 Effect。
    """
    from compose.tool_layer import layer_program

    effects, _ = _drive_program(layer_program("memory", payload={"layer": "context"}, output="DONE")())
    assert len(effects) == 2
    assert effects[0].port == "memory"
    assert effects[0].payload == {"layer": "context"}
    assert effects[1].output == "DONE"


def test_program01_as_program_wraps_effect_into_constant_program() -> None:
    """输入：Terminate(output='C') 常量 + 一个已有 ProgramFn。
    真实入口：compose.program._as_program（常量分支的 constant 闭包）。
    精确断言：常量经工厂驱动产出 Terminate(output='C')；已有工厂被原样返回（同一对象）。
    失败判据：常量未被包装、产出不符或原工厂被替换。
    """
    from compose.program import _as_program

    factory = _as_program(Terminate(output="C"))
    effects, _ = drive(factory)
    assert len(effects) == 1
    assert effects[0].output == "C"

    original = _term_program("keep")
    assert _as_program(original) is original


def test_program02_branch_value_routes_source_output_and_honors_no_route_reason() -> None:
    """输入：source 产出 {'intent':'b'}；arms={'a':..., 'b':...}；fn 取 intent。
    真实入口：compose.program.branch_value(...).program（含 _as_program 常量臂）。
    精确断言：命中臂产出 'arm-b'；无匹配臂产出 output=None、reason='custom-no-route'。
    失败判据：分支选择错臂或无路由 reason 不符。
    """
    from compose.program import branch_value

    def source():
        yield Terminate(output={"intent": "b"})
        return None

    def arm(label: str) -> Any:
        def program():
            yield Terminate(output=label)
            return None

        return program

    routed = branch_value(source, lambda value: value["intent"], {"a": arm("arm-a"), "b": arm("arm-b")})
    effects, _ = drive(routed)
    assert effects[-1].output == "arm-b"

    unrouted = branch_value(source, lambda value: "missing", {}, no_route_reason="custom-no-route")
    effects2, _ = drive(unrouted)
    assert effects2[-1].output is None
    assert effects2[-1].reason == "custom-no-route"


# =========================================================================== #
# harness.context.manager（7）
# =========================================================================== #
def test_ctxmgr01_record_cache_hit_and_miss_update_telemetry(loop: Any) -> None:
    """输入：新 Manager 连续记 2 命中 1 未命中（label='llm:primary'）再记 1 默认未命中。
    真实入口：ContextMemoryManager.record_cache_hit/record_cache_miss（经 CacheTelemetry）。
    精确断言：hits=2、misses=1、total=3、hit_rate=2/3、label='llm:primary'；默认 label 后续累计 total=4。
    失败判据：计数/命中率不符或 label 未归因。
    """
    from harness.context.manager import ContextMemoryManager

    mgr = ContextMemoryManager()
    assert mgr.cache_telemetry.total == 0
    mgr.record_cache_hit("llm:primary")
    mgr.record_cache_miss("llm:primary")
    mgr.record_cache_hit("llm:primary")
    telemetry = mgr.cache_telemetry
    assert (telemetry.hits, telemetry.misses, telemetry.total) == (2, 1, 3)
    assert telemetry.label == "llm:primary"
    assert telemetry.hit_rate == 2 / 3
    mgr.record_cache_miss()
    assert mgr.cache_telemetry.label == "default"
    assert mgr.cache_telemetry.total == 4


def test_ctxmgr02_build_yields_memory_layer_program() -> None:
    """输入：新 Manager.build() 产出的 Program。
    真实入口：ContextMemoryManager.build（内部 layer_program('memory')）。
    精确断言：Invoke(port='memory', payload={'layer':'context'}) → Terminate(output=None)。
    失败判据：端口或 payload 不符。
    """
    from harness.context.manager import ContextMemoryManager

    effects, _ = _drive_program(ContextMemoryManager().build())
    assert len(effects) == 2
    assert effects[0].port == "memory"
    assert effects[0].payload == {"layer": "context"}
    assert effects[1].output is None


def test_ctxmgr03_assemble_delegates_with_exact_budget_trimming() -> None:
    """输入：system/history/task 三段（含超 quota 段）+ TokenBudget 上限 100 与 22。
    真实入口：ContextMemoryManager.assemble(sections, budget)。
    精确断言：按 priority 排序、system 仅首份、TAIL/HEAD 裁剪正确；紧预算下最后一段被裁到剩余额度。
    失败判据：段顺序/裁剪策略/预算扣减不符。
    """
    from harness.context.manager import ContextMemoryManager
    from harness.context.types import ContextSection, TrimPolicy

    mgr = ContextMemoryManager()
    sections = [
        ContextSection("system", "SYS" * 10, 100, 0, TrimPolicy.PIN),
        ContextSection("history", "abcdef", 10, 2, TrimPolicy.HEAD),
        ContextSection("task", "xyzTAIL", 20, 1, TrimPolicy.TAIL),
        ContextSection("system", "INJECTED", 100, 0, TrimPolicy.PIN),
    ]
    roomy = mgr.assemble(sections, TokenBudget(limit=100))
    assert [(m.role, m.content) for m in roomy] == [
        ("system", "SYS" * 10),
        ("task", "xyzTAIL"),
        ("history", "abcdef"),
    ]

    tight = mgr.assemble(sections, TokenBudget(limit=22))
    assert [(m.role, m.content) for m in tight] == [
        ("system", "SYSSYSSYSSY"),
        ("task", "xyzTAIL"),
        ("history", "abcd"),
    ]


def test_ctxmgr04_commit_and_compact_writes_cold_then_compacts_and_reinjects() -> None:
    """输入：6 条 messages 的 State + transcript + tasks + 预算 100。
    真实入口：ContextMemoryManager.commit_and_compact(state, transcript, ctx, budget, tasks)。
    精确断言：trace=['write_cold','compact']；原文以 cold:transcript 落库（由 forget_expired 精确读回）；压缩插摘要并按预算回注摘要与钉住任务；State.messages 不变。
    失败判据：顺序不对、原文未落库、摘要或 tasks 缺失、原 State 被改。
    """
    from harness.context.manager import ContextMemoryManager

    state = AgentState(
        messages=(
            Message(role="system", content="SYS"),
            Message(role="user", content="old"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="a2"),
            Message(role="assistant", content="a3"),
            Message(role="user", content="latest"),
        ),
        budgets=Budgets(max_tokens=100, used_tokens=0),
    )
    mgr = ContextMemoryManager()
    view = mgr.commit_and_compact(
        state, "full transcript here", PortCtx(session_id="s"),
        TokenBudget(limit=100), tasks=("todo:finish",),
    )
    assert mgr.trace == ["write_cold", "compact"]
    assert len(state.messages) == 6
    assert [m.role for m in view] == [
        "system", "summary", "assistant", "user", "assistant", "user", "summary", "pinned",
    ]
    assert view[1].content == "[摘要] 1 条历史 (user)"
    assert view[-1].content == "todo:finish"
    expired = mgr.memory.forget_expired(ttl=0, now=10**20)
    assert len(expired) == 1
    assert expired[0].content == "full transcript here"
    assert expired[0].source == "cold:transcript"


def test_ctxmgr05_offload_returns_handle_readable_from_blob(loop: Any) -> None:
    """输入：dict 大结果 + PortCtx。
    真实入口：ContextMemoryManager.offload(value, ctx)（异步）+ manager.blob.get。
    精确断言：句柄以 store:// 开头，按句柄读回原 dict 精确相等。
    失败判据：句柄不可读回或读回值被改写。
    """
    from harness.context.manager import ContextMemoryManager

    mgr = ContextMemoryManager()
    value = {"big": "x" * 10}
    handle = run(mgr.offload(value, PortCtx(session_id="s")), loop)
    assert handle.startswith("store://")
    assert run(mgr.blob.get(handle), loop) == value


def test_ctxmgr06_with_replaces_dependency_and_preserves_history(loop: Any) -> None:
    """输入：先记录一次命中/一条 trace，再用新 token_counter 与 memory 调 with_。
    真实入口：ContextMemoryManager.with_(...)。
    精确断言：返回新实例且新 counter 同时注入 assembler/compaction/reinjector；memory 被替换；trace 拷贝、telemetry 共享；旧实例不变。
    失败判据：依赖未替换、旧实例被改或观测历史丢失。
    """
    from harness.context.manager import ContextMemoryManager
    from harness.memory.memory import MemoryStore

    class _Count7:
        name = "count7"

        def count(self, text: str) -> int:
            return 7

    mgr = ContextMemoryManager()
    mgr.record_cache_hit("before")
    mgr.trace.append("custom")
    old_counter = mgr.token_counter
    counter = _Count7()
    new_memory = MemoryStore()

    new = mgr.with_(token_counter=counter, memory=new_memory)
    assert new is not mgr
    assert new.token_counter is counter
    assert new.assembler._counter is counter
    assert new.compaction._counter is counter
    assert new.reinjector._counter is counter
    assert new.memory is new_memory
    assert new.trace == ["custom"]
    assert new.cache_telemetry is mgr.cache_telemetry
    assert mgr.token_counter is old_counter
    assert mgr.memory is not new_memory
    assert mgr.trace == ["custom"]


# =========================================================================== #
# harness.context.offloader（6）
# =========================================================================== #
def test_offloader01_handle_roundtrip_and_store_lifecycle(loop: Any) -> None:
    """输入：blob_id='abc'；错误 scheme 与空 id 句柄；put/get/delete。
    真实入口：make_handle/parse_handle/InMemoryBlobStore.put/get/delete/DefaultOffloader.offload。
    精确断言：句柄包装与解析精确、错误句柄解析为 None；put→get 精确读回、delete→get=None；offload 借 store 落库且可读回。
    失败判据：scheme 不符、空 id 解析错误或删除后仍可读。
    """
    from harness.context.offloader import (
        DefaultOffloader, InMemoryBlobStore, make_handle, parse_handle,
    )

    assert make_handle("abc") == "store://abc"
    assert parse_handle("store://abc") == "abc"
    assert parse_handle("http://abc") is None
    assert parse_handle("store://") is None

    store = InMemoryBlobStore()
    handle = run(store.put({"v": 1}), loop)
    assert parse_handle(handle) == handle[len("store://"):]
    assert run(store.get(handle), loop) == {"v": 1}
    assert run(store.get("bad"), loop) is None
    run(store.delete(handle), loop)
    assert run(store.get(handle), loop) is None
    run(store.delete(handle), loop)

    offloader = DefaultOffloader()
    handle2 = run(offloader.offload([1, 2], store), loop)
    assert run(store.get(handle2), loop) == [1, 2]


# =========================================================================== #
# harness.context.observability（5）
# =========================================================================== #
def test_observability01_cache_telemetry_is_immutable_and_labelable() -> None:
    """输入：空 CacheTelemetry → record_hit/miss/with_label；负数计数。
    真实入口：CacheTelemetry.total/hit_rate/record_hit/record_miss/with_label。
    精确断言：total/hit_rate 精确（含 0 次访问→0.0）；记录返回新实例不改原对象；with_label 保计数换标签；负计数 ValueError。
    失败判据：原地修改、命中率 NaN/None 或负计数被接受。
    """
    from harness.context.observability import CacheTelemetry

    empty = CacheTelemetry()
    assert (empty.hits, empty.misses, empty.total, empty.hit_rate, empty.label) == (
        0, 0, 0, 0.0, "default"
    )
    updated = empty.record_hit().record_miss().record_hit()
    assert (updated.hits, updated.misses, updated.total) == (2, 1, 3)
    assert updated.hit_rate == 2 / 3
    assert (empty.hits, empty.misses) == (0, 0)
    labeled = updated.with_label("prefix:system")
    assert (labeled.label, labeled.hits, labeled.misses) == ("prefix:system", 2, 1)
    with pytest.raises(ValueError, match="non-negative"):
        CacheTelemetry(hits=-1)


# =========================================================================== #
# harness.context.compaction（3）+ token_counter（2）
# =========================================================================== #
def test_compaction01_count_tokens_should_compact_and_compact_view() -> None:
    """输入：中文/ASCII 混排文本；used_tokens=60/max=100 与 used_tokens=0 两态 State；4 条 messages。
    真实入口：count_tokens / DefaultCompactionPolicy.should_compact / compact。
    精确断言：count_tokens('中文abc')==5；阈值触发 True/False；压缩视图 roles 精确且摘要正文精确，入参未被改。
    失败判据：计数口径不符、阈值判定反了或摘要内容/位置错。
    """
    from harness.context.compaction import (
        DefaultCompactionPolicy, SUMMARY_ROLE, count_tokens,
    )

    assert count_tokens("中文abc") == 5
    policy = DefaultCompactionPolicy(trigger_tokens=0.5, trigger_turns=99, keep_recent=2)
    messages = (
        Message(role="system", content="S"),
        Message(role="user", content="old"),
        Message(role="user", content="mid"),
        Message(role="assistant", content="new"),
    )
    hot = AgentState(messages=messages, budgets=Budgets(max_tokens=100, used_tokens=60))
    cold = AgentState(messages=messages, budgets=Budgets(max_tokens=100, used_tokens=0))
    assert policy.should_compact(hot) is True
    assert policy.should_compact(cold) is False

    view = policy.compact(messages)
    assert [m.role for m in view] == ["system", SUMMARY_ROLE, "user", "assistant"]
    assert view[1].content == "[摘要] 1 条历史 (user)"
    assert len(messages) == 4


def test_token_counter01_estimate_and_default_counter_share_exact_cost() -> None:
    """输入：'' 与 'abc中文' 两段文本。
    真实入口：estimate_tokens / DefaultTokenCounter.count。
    精确断言：''→0、'abc中文'→5，两个入口返回值完全相同。
    失败判据：口径不一致或空串未按 0 计。
    """
    from harness.context.token_counter import DefaultTokenCounter, estimate_tokens

    assert estimate_tokens("") == 0
    assert estimate_tokens("abc中文") == 5
    assert DefaultTokenCounter().count("abc中文") == estimate_tokens("abc中文") == 5


# =========================================================================== #
# harness.context.assembler（1）+ reinjector（1）
# =========================================================================== #
def test_assembler01_assemble_dedups_system_and_trims_by_quota() -> None:
    """输入：两条 system 段 + 两条普通段（HEAD/TAIL 超配额），预算不限。
    真实入口：DefaultContextAssembler.assemble(sections, budget)。
    精确断言：system 只保留先到者；HEAD 取头、TAIL 取尾；输出 role/content 精确。
    失败判据：重复 system 被保留或裁剪方向错误。
    """
    from harness.context.assembler import DefaultContextAssembler
    from harness.context.types import ContextSection, TrimPolicy

    sections = [
        ContextSection("system", "FIRST", 10, 0, TrimPolicy.PIN),
        ContextSection("system", "SECOND", 10, 0, TrimPolicy.PIN),
        ContextSection("head", "abcdef", 3, 1, TrimPolicy.HEAD),
        ContextSection("tail", "uvwxyz", 3, 2, TrimPolicy.TAIL),
    ]
    out = DefaultContextAssembler().assemble(sections, TokenBudget(limit=None))
    assert [(m.role, m.content) for m in out] == [
        ("system", "FIRST"), ("head", "abc"), ("tail", "xyz"),
    ]


def test_reinjector01_reinject_skips_overflow_and_keeps_budget() -> None:
    """输入：2 条 view + summary=16 字 + tasks=('todo:finish',)+ 预算 20（extra=6）。
    真实入口：DefaultReinjector.reinject(view, summary, tasks, budget)。
    精确断言：超预算的 summary 与 tasks 均被跳过，输出恰为原 view；预算 100 时两者都回注且 role 精确。
    失败判据：绕过预算回注或回注项角色错位。
    """
    from harness.context.reinjector import DefaultReinjector

    view = (Message(role="system", content="SYS"), Message(role="user", content="q"))
    summary = Message(role="summary", content="[摘要] 1 条历史 (user)")
    tasks = ("todo:finish",)
    small = DefaultReinjector().reinject(view, summary, tasks, TokenBudget(limit=20))
    assert small == view

    large = DefaultReinjector().reinject(view, summary, tasks, TokenBudget(limit=100))
    assert [(m.role, m.content) for m in large] == [
        ("system", "SYS"),
        ("user", "q"),
        ("summary", "[摘要] 1 条历史 (user)"),
        ("pinned", "todo:finish"),
    ]


# =========================================================================== #
# harness.memory.memory（7）
# =========================================================================== #
def test_memory01_forget_removes_kv_entry_idempotently(loop: Any) -> None:
    """输入：write('ns','k','value') 后 forget 两次。
    真实入口：MemoryStore.write / forget / read。
    精确断言：写入后 read='value'；一次 forget 后 read=None；二次 forget 幂等不抛。
    失败判据：遗忘未生效或二次遗忘抛错。
    """
    from harness.memory.memory import MemoryStore

    ms = MemoryStore()
    ctx = PortCtx(session_id="s")
    run(ms.write("ns", "k", "value", ctx), loop)
    assert run(ms.read("ns", "k", ctx), loop) == "value"
    run(ms.forget("ns", "k", ctx), loop)
    assert run(ms.read("ns", "k", ctx), loop) is None
    run(ms.forget("ns", "k", ctx), loop)


def test_memory02_write_cold_stores_full_transcript_as_cold_item() -> None:
    """输入：write_cold('translate full text', ctx)。
    真实入口：MemoryStore.write_cold（配合 forget_expired 观察落库结果）。
    精确断言：过期扫出唯一项，content/source/kind 精确，tier_count 归零。
    失败判据：未落库、正文被提炼改写或 source 不是 cold:transcript。
    """
    from harness.memory.memory import MemoryStore

    ms = MemoryStore()
    ms.write_cold("translate full text", PortCtx(session_id="s"))
    expired = ms.forget_expired(ttl=0, now=10**20)
    assert [(i.content, i.source, i.kind.value) for i in expired] == [
        ("translate full text", "cold:transcript", "episodic")
    ]
    assert ms.tier_count() == 0


def test_memory03_tier_of_promote_demote_move_one_level_with_clamps() -> None:
    """输入：put_item(item, COLD) 后连续 promote/demote。
    真实入口：MemoryStore.tier_of / promote / demote。
    精确断言：cold→warm→hot→hot（钳位）、hot→warm→cold→cold（钳位），每步 tier_of 精确。
    失败判据：晋升/沉降方向反了或越界未钳位。
    """
    from harness.memory.memory import MemoryStore
    from harness.memory.types import MemoryItem, MemoryKind, MemoryTier

    ms = MemoryStore()
    item = MemoryItem(MemoryKind.SEMANTIC, "fact-x", 0.7, 100, "test", item_id="tier-1")
    ms.put_item(item, MemoryTier.COLD)
    assert ms.tier_of(item) is MemoryTier.COLD
    ms.promote(item)
    assert ms.tier_of(item) is MemoryTier.WARM
    ms.promote(item)
    assert ms.tier_of(item) is MemoryTier.HOT
    ms.promote(item)
    assert ms.tier_of(item) is MemoryTier.HOT
    ms.demote(item)
    assert ms.tier_of(item) is MemoryTier.WARM
    ms.demote(item)
    assert ms.tier_of(item) is MemoryTier.COLD
    ms.demote(item)
    assert ms.tier_of(item) is MemoryTier.COLD


def test_memory04_forget_expired_uses_ttl_and_now_exactly() -> None:
    """输入：fresh(ts=1000) 与 stale(ts=0) 放入 HOT，ttl=500、now=1000。
    真实入口：MemoryStore.forget_expired(ttl=..., now=...)。
    精确断言：仅 stale 过期返回，fresh 仍在库；过期项 tier_of=None。
    失败判据：边界判错或两项一起被清。
    """
    from harness.memory.memory import MemoryStore
    from harness.memory.types import MemoryItem, MemoryKind, MemoryTier

    ms = MemoryStore()
    fresh = MemoryItem(MemoryKind.EPISODIC, "新鲜", 0.5, 1000, "test", item_id="fresh")
    stale = MemoryItem(MemoryKind.EPISODIC, "过期", 0.5, 0, "test", item_id="stale")
    ms.put_item(fresh, MemoryTier.HOT, now=1000)
    ms.put_item(stale, MemoryTier.HOT, now=0)
    expired = ms.forget_expired(ttl=500, now=1000)
    assert [(i.content, i.item_id) for i in expired] == [("过期", "stale")]
    assert ms.tier_of(stale) is None
    assert ms.tier_of(fresh) is MemoryTier.HOT


def test_memory05_importance_of_reads_stored_score_after_access() -> None:
    """输入：显式 item_id 的 MemoryItem(importance=0.5) 入库后 record_access 一次。
    真实入口：MemoryStore.importance_of（配合 record_access 观察重算）。
    精确断言：初始 importance=0.5；命中后为 0.5 + min(0.2, 0.02*log(2)) 的精确浮点值。
    失败判据：读取的是原始对象而非库内更新值，或增量公式不符。
    """
    from harness.memory.memory import MemoryStore
    from harness.memory.types import MemoryItem, MemoryKind, MemoryTier

    ms = MemoryStore()
    item = MemoryItem(MemoryKind.SEMANTIC, "imp", 0.5, 1, "test", item_id="imp-1")
    ms.put_item(item, MemoryTier.WARM)
    assert ms.importance_of(item) == 0.5
    ms.record_access(item)
    expected = 0.5 + min(0.2, 0.02 * math.log(2))
    assert ms.importance_of(item) == expected


# =========================================================================== #
# harness.rag.pipeline（6）/ rerank（2）/ retriever（2）/ rewrite（2）/ store（1）
# =========================================================================== #
def _builtin_rag() -> Any:
    from harness.rag import (
        DefaultPostProcessor, HashEmbedder, InMemoryVectorStore,
        RAGPipeline, RecursiveSplitter, VectorRetriever,
    )

    store = InMemoryVectorStore()
    return RAGPipeline(
        splitter=RecursiveSplitter(chunk_size=40, overlap=8),
        embedder=HashEmbedder(dim=16), store=store,
        retriever=VectorRetriever(store), post=DefaultPostProcessor(),
    )


def test_rag_cache01_inmemory_cache_set_get_and_invalidate() -> None:
    """输入：RAGResult('ctx', ['d:1'])；键 'k'/'a'/'b'。
    真实入口：InMemoryCache.__init__/get/set/invalidate（单键与全清）。
    精确断言：未命中 None、命中返回同一对象、单键失效后 None、无参全清两个键。
    失败判据：对象被复制、单键失效误伤其他键或全清未生效。
    """
    from harness.rag.pipeline import InMemoryCache

    cache = InMemoryCache()
    result = RAGResult(context="ctx", citations=["d:1"])
    assert cache.get("k") is None
    cache.set("k", result)
    assert cache.get("k") is result
    cache.invalidate("k")
    assert cache.get("k") is None
    cache.set("a", result)
    cache.set("b", result)
    cache.invalidate()
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_rag_pipeline01_build_and_with_replace_injections() -> None:
    """输入：内置 RAGPipeline + LexicalReranker 替换 + 未知替换点。
    真实入口：RAGPipeline.build / with_。
    精确断言：build 产出 Invoke(port='rag', payload={'layer':'rag'})→Terminate；with_ 真替换 rerank、保留 splitter/store，未知点 ValueError 消息精确。
    失败判据：Program 端口错、替换未生效或未知点被静默接受。
    """
    from harness.rag import LexicalReranker

    pipeline = _builtin_rag()
    effects, _ = _drive_program(pipeline.build())
    assert len(effects) == 2
    assert effects[0].port == "rag"
    assert effects[0].payload == {"layer": "rag"}
    assert effects[1].output is None

    reranker = LexicalReranker(top_n=2)
    new = pipeline.with_(rerank=reranker)
    assert new.rerank is reranker
    assert pipeline.rerank is None
    assert new.splitter is pipeline.splitter
    assert new.store is pipeline.store
    with pytest.raises(ValueError, match="Unknown replacement points"):
        pipeline.with_(bogus=1)


def test_rag_rerank01_lexical_reranker_orders_by_coverage_and_caps_top_n() -> None:
    """输入：3 个 Hit（apple / banana / banana pear）+ Query('banana pear') + top_n=2。
    真实入口：LexicalReranker.__init__/rerank。
    精确断言：覆盖度 1 的 'banana pear' 排首、0.5 的 'banana' 次之，'apple' 被 top_n 截掉。
    失败判据：排序未按词项覆盖度或 top_n 未生效。
    """
    from harness.rag.rerank import LexicalReranker

    def hit(cid: str, text: str) -> Hit:
        return Hit(chunk=Chunk(chunk_id=cid, doc_id="d", text=text), score=0.0, source="vec")

    hits = [hit("a", "apple"), hit("b", "banana"), hit("c", "banana pear")]
    reranker = LexicalReranker(top_n=2)
    assert reranker.top_n == 2
    ranked = reranker.rerank(hits, Query(text="banana pear"))
    assert [h.chunk.chunk_id for h in ranked] == ["c", "b"]


def test_rag_retriever01_hybrid_retriever_fuses_vector_and_keyword_channels(loop: Any) -> None:
    """输入：store 内两块（redis caching / config cache）+ Query('cache', top_k=2)；另一路只给 all_chunks 外部语料。
    真实入口：HybridRetriever.__init__/retrieve（真 store.search + RRF）。
    精确断言：双通道命中块 id 精确；'config cache' 向量通道 rank2 + 词面通道 rank1 得 1/62+1/61，'redis caching' 仅向量通道 rank1 得 1/61，前者排首；显式 all_chunks 的关键词块即使不在 store 也被召回。
    失败判据：RRF 分数/顺序不符或关键词通道绕过 all_chunks。
    """
    from harness.rag.retriever import HybridRetriever
    from harness.rag.store import InMemoryVectorStore

    store = InMemoryVectorStore()
    c1 = Chunk(chunk_id="d:1", doc_id="d", text="redis caching", vector=(1.0, 0.0))
    c2 = Chunk(chunk_id="d:2", doc_id="d", text="config cache", vector=(0.6, 0.8))
    c3 = Chunk(chunk_id="ext:1", doc_id="ext", text="cache external", vector=(0.0, 0.0))
    run(store.upsert([c1, c2]), loop)

    retriever = HybridRetriever(store)
    hits = run(retriever.retrieve(Query(text="cache", top_k=2), (1.0, 0.0)), loop)
    assert [h.chunk.chunk_id for h in hits] == ["d:2", "d:1"]
    assert hits[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert hits[1].score == pytest.approx(1 / 61)

    external = HybridRetriever(store, all_chunks=[c3])
    outside = run(external.retrieve(Query(text="cache", top_k=5), (0.0, 0.0)), loop)
    assert "ext:1" in [h.chunk.chunk_id for h in outside]


def test_rag_rewrite01_multi_query_rewriter_emits_exact_variants(loop: Any) -> None:
    """输入：自定义同义词 {'vector': ('embedding',)} 与查询 'vector search'；n_variants=1 边界。
    真实入口：MultiQueryRewriter.__init__/rewrite。
    精确断言：变体文本精确为 ['vector search','embedding search']；n_variants=1 只产原串；其余字段继承。
    失败判据：引入同义词表外的新词或截断数量不符。
    """
    from harness.rag.rewrite import MultiQueryRewriter

    rewriter = MultiQueryRewriter(n_variants=3, synonyms={"vector": ("embedding",)})
    variants = run(rewriter.rewrite(Query(text="vector search", top_k=7)), loop)
    assert [q.text for q in variants] == ["vector search", "embedding search"]
    assert all(q.top_k == 7 for q in variants)
    single = run(MultiQueryRewriter(n_variants=1, synonyms={"vector": ("embedding",)}).rewrite(
        Query(text="vector search")), loop)
    assert [q.text for q in single] == ["vector search"]


def test_rag_store01_all_chunks_returns_live_list_copy(loop: Any) -> None:
    """输入：upsert 两块后取 all_chunks 并清空返回列表。
    真实入口：InMemoryVectorStore.all_chunks。
    精确断言：首次返回顺序精确；清空返回列表不影响 store 内数据（第二次仍两块）。
    失败判据：返回内部列表导致外部可改库。
    """
    from harness.rag.store import InMemoryVectorStore

    store = InMemoryVectorStore()
    c1 = Chunk(chunk_id="d:1", doc_id="d", text="one")
    c2 = Chunk(chunk_id="d:2", doc_id="d", text="two")
    run(store.upsert([c1, c2]), loop)
    listed = store.all_chunks()
    assert listed == [c1, c2]
    listed.clear()
    assert store.all_chunks() == [c1, c2]


# =========================================================================== #
# harness.tools.execution（7）/ capability（3）/ registry（3）/ decorator（1）
# =========================================================================== #
class _Sandbox:
    """真执行 fn 的沙盒替身：记录交进来的 args，可注入异常。"""

    name = "sandbox"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def run(self, fn: Any, args: dict[str, Any], ctx: PortCtx) -> Any:
        self.calls.append(dict(args))
        if self.fail:
            raise RuntimeError("sandbox exploded")
        return fn(**args)


def test_tools_execution01_code_exec_routes_through_sandbox_and_fails_closed(loop: Any) -> None:
    """输入：注入 _Sandbox 的 CodeExecTool；另取无 sandbox 与抛异常 sandbox 的实例。
    真实入口：CodeExecTool.__init__/execute（含 _ExecTool.__init__/_run）。
    精确断言：execute('print(1)')→True 且 sandbox.calls=[{'code':'print(1)'}]；无 sandbox→False 零调用；异常 sandbox→False。
    失败判据：高危执行裸跑、异常抛穿或调用参数不符。
    """
    from harness.tools.execution import CodeExecTool

    sandbox = _Sandbox()
    tool = CodeExecTool(sandbox=sandbox)
    assert tool.spec.name == "code_exec"
    assert run(tool.execute("print(1)", PortCtx()), loop) is True
    assert sandbox.calls == [{"code": "print(1)"}]

    no_sandbox = CodeExecTool()
    assert run(no_sandbox.execute("print(1)", PortCtx()), loop) is False

    boom = CodeExecTool(sandbox=_Sandbox(fail=True))
    assert run(boom.execute("x", PortCtx()), loop) is False


def test_tools_execution02_shell_allowlist_and_file_virtual_root(loop: Any, tmp_path: Any) -> None:
    """输入：ShellExecTool(allowlist=('ls',)) 的 ls/rm；FileExecTool(root=tmp_path) 的正常/越界/空根读。
    真实入口：ShellExecTool.__init__/execute、FileExecTool.__init__/read。
    精确断言：ls 放行且 sandbox.calls 精确，rm 零调用；正常文件读放行、../ 越界与空根零调用。
    失败判据：白名单/虚拟根被绕过或 sandbox 被无谓调用。
    """
    from harness.tools.execution import FileExecTool, ShellExecTool

    shell_sandbox = _Sandbox()
    shell = ShellExecTool(sandbox=shell_sandbox, allowlist=("ls",))
    assert run(shell.execute("ls -la", PortCtx()), loop) is True
    assert shell_sandbox.calls == [{"command": "ls -la"}]
    assert run(shell.execute("rm -rf /", PortCtx()), loop) is False
    assert shell_sandbox.calls == [{"command": "ls -la"}]

    file_sandbox = _Sandbox()
    root = str(tmp_path)
    file_tool = FileExecTool(sandbox=file_sandbox, root=root)
    assert run(file_tool.read("data.txt", PortCtx()), loop) is True
    assert file_sandbox.calls == [{"path": os.path.join(root, "data.txt")}]
    assert run(file_tool.read("../../etc/passwd", PortCtx()), loop) is False
    assert file_sandbox.calls == [{"path": os.path.join(root, "data.txt")}]
    empty_root = FileExecTool(sandbox=_Sandbox(), root="")
    assert run(empty_root.read("anything", PortCtx()), loop) is False


def test_tools_capability01_build_and_optional_mcp_skills_accessors() -> None:
    """输入：注入 MCPManager 与 SkillPort 的 ToolsCapability。
    真实入口：ToolsCapability.build / manager / skills。
    精确断言：build 产出 Invoke(port='tool', payload={'layer':'tools'})→Terminate；manager/skills 返回注入对象；未注入时均为 None。
    失败判据：Program 端口错或注入对象丢失。
    """
    from harness.tools import MCPManager, ToolRegistry, ToolsCapability

    class _Transport:
        name = "transport"

        async def list_tools(self, server: str) -> list[ToolSpec]:
            return []

        async def call_tool(self, server: str, tool: str, args: dict[str, Any]) -> Any:
            return None

        async def ping(self, server: str) -> bool:
            return True

    class _Skills:
        name = "skills"

        def catalog(self) -> list[Any]:
            return []

    manager = MCPManager(ToolRegistry(), _Transport())  # type: ignore[arg-type]
    skills = _Skills()
    cap = ToolsCapability(mcp=manager, skills=skills)  # type: ignore[arg-type]
    assert cap.manager() is manager
    assert cap.skills() is skills
    effects, _ = _drive_program(cap.build())
    assert len(effects) == 2
    assert effects[0].port == "tool"
    assert effects[0].payload == {"layer": "tools"}
    assert effects[1].output is None

    bare = ToolsCapability()
    assert bare.manager() is None
    assert bare.skills() is None


def test_tools_registry01_bind_static_check_and_freeze(loop: Any) -> None:
    """输入：SAFE 工具 'add'；另一个缺沙盒的 HIGH 工具 'hi'。
    真实入口：ToolRegistry.bind / static_check / freeze。
    精确断言：bind 后 call 返回替换实现的值 12；static_check 精确列出 'hi: HIGH tool missing sandbox'；freeze 返回 self 并拒绝 register，但 bind 仍可热更。
    失败判据：替换未生效、问题清单不符或冻结语义错误。
    """
    from harness.tools import ToolRegistry

    reg = ToolRegistry()
    spec = ToolSpec(
        name="add", description="add", danger=DangerLevel.SAFE,
        parameters_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    reg.register(spec, lambda x: x + 1)
    assert reg.bind("add", lambda x: x + 10) is None
    result = run(reg.call(ToolCall(call_id="c1", name="add", args={"x": 2}), PortCtx()), loop)
    assert result.ok is True and result.value == 12
    assert reg.static_check() == []

    unsafe = ToolRegistry()
    unsafe.register(
        ToolSpec(name="hi", description="high", danger=DangerLevel.HIGH,
                 parameters_schema={"type": "object"}),
        lambda: None,
    )
    assert unsafe.static_check() == ["hi: HIGH tool missing sandbox"]

    assert reg.freeze() is reg
    with pytest.raises(RuntimeError, match="registry tools is frozen"):
        reg.register(ToolSpec(name="later", danger=DangerLevel.SAFE), lambda: None)
    assert reg.bind("add", lambda x: 99) is None
    after = run(reg.call(ToolCall(call_id="c2", name="add", args={"x": 2}), PortCtx()), loop)
    assert after.ok is True and after.value == 99


def test_tools_decorator01_tool_builds_spec_registers_pending_and_executes() -> None:
    """输入：@tool(name='mul', danger=SAFE, description=...) 装饰 def mul(x:int,y:int)->int。
    真实入口：harness.tools.tool → ToolFn/spec/pending_tools/__call__。
    精确断言：spec.name/description/schema/danger/source 精确；pending_tools 顺序精确；mul(x=3,y=4)==12；缺参 TypeError。
    失败判据：schema 推导错、未入 pending 或执行体参数未校验。
    """
    from harness.tools import pending_tools, tool

    @tool(name="mul", danger=DangerLevel.SAFE, description="Multiply two numbers")
    def mul(x: int, y: int) -> int:
        """unused docstring"""
        return x * y

    assert mul.spec.name == "mul"
    assert mul.spec.description == "Multiply two numbers"
    assert mul.spec.parameters_schema["properties"] == {
        "x": {"type": "integer"}, "y": {"type": "integer"},
    }
    assert mul.spec.parameters_schema["required"] == ["x", "y"]
    assert mul.spec.danger is DangerLevel.SAFE
    assert mul.spec.source == "local"
    assert [t.spec.name for t in pending_tools()] == ["mul"]
    assert mul(x=3, y=4) == 12
    with pytest.raises(TypeError):
        mul(x=3)


# =========================================================================== #
# ports.adapters.mcp（7）+ ports.adapters.provider（2）
# =========================================================================== #
class _FakeStdioChannel:
    """内存 stdio 通道：记录真实发出的 JSON-RPC 帧，按序返回预设响应。"""

    name = "fake_stdio_channel"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def recv(self) -> dict[str, Any]:
        if not self.responses:
            raise RuntimeError("no more responses")
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def _mcp_responses() -> list[dict[str, Any]]:
    """握手 → tools/list → tools/call → ping 的标准 JSON-RPC 响应序列。"""
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1"},
        }},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
            "name": "search", "description": "搜索网页",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }]}},
        {"jsonrpc": "2.0", "id": 3, "result": {
            "content": [{"type": "text", "text": "hit"}], "isError": False,
        }},
        {"jsonrpc": "2.0", "id": 4, "result": {}},
    ]


def test_mcp01_stdio_transport_real_frames_and_reconnect(loop: Any) -> None:
    """输入：脚本化 stdio 通道 + StdioMCPTransport。
    真实入口：StdioMCPTransport.list_tools/call_tool/ping/reconnect/aclose（含真握手帧）。
    精确断言：帧方法序列 initialize→notifications/initialized→tools/list→tools/call→ping；ToolSpec 字段精确、call_tool 返回 'hit'、ping True；reconnect 关通道后下一次 ping 重新握手。
    失败判据：帧顺序/方法不符、工具映射错或重连后未重新 initialize。
    """
    from ports.adapters.mcp import StdioMCPTransport

    channel = _FakeStdioChannel(_mcp_responses())
    transport = StdioMCPTransport(channel)
    specs = run(transport.list_tools("srv"), loop)
    assert [s.name for s in specs] == ["search"]
    assert specs[0].description == "搜索网页"
    assert specs[0].parameters_schema["properties"]["q"]["type"] == "string"
    assert specs[0].source == "mcp"
    assert specs[0].danger is DangerLevel.SAFE
    assert [m["method"] for m in channel.sent] == [
        "initialize", "notifications/initialized", "tools/list",
    ]

    assert run(transport.call_tool("srv", "search", {"q": "x"}), loop) == "hit"
    assert run(transport.ping("srv"), loop) is True
    assert channel.sent[-2]["method"] == "tools/call"
    assert channel.sent[-2]["params"] == {"name": "search", "arguments": {"q": "x"}}
    assert channel.sent[-1]["method"] == "ping"
    assert "params" not in channel.sent[-1]

    run(transport.reconnect(), loop)
    assert channel.closed is True
    channel.responses.extend(_mcp_responses())
    assert run(transport.ping("srv"), loop) is True
    assert [m["method"] for m in channel.sent].count("initialize") == 2
    run(transport.aclose(), loop)
    assert channel.closed is True


def test_mcp02_http_transport_via_mock_transport(loop: Any) -> None:
    """输入：httpx.MockTransport 捕获 POST 帧并按序回 JSON-RPC 响应 + HttpMCPTransport。
    真实入口：HttpMCPTransport.__init__/list_tools/call_tool/ping/reconnect/aclose。
    精确断言：捕获帧方法序列与参数精确、ToolSpec 字段、call 返回 'hit'、ping True；reconnect 后重新 initialize；注入 client 在 aclose 后仍可用。
    失败判据：HTTP 帧未按 JSON-RPC 构造、重连未重握手或注入 client 被误关。
    """
    from ports.adapters.mcp import HttpMCPTransport

    captured: list[dict[str, Any]] = []
    queue = _mcp_responses()

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:  # 后续存活探针（GET）无 JSON 帧
            return httpx.Response(500, text="alive")
        frame = json.loads(request.content)
        captured.append(frame)
        if "id" not in frame:
            return httpx.Response(202, text="")
        if not queue:
            return httpx.Response(500, text="no more")
        return httpx.Response(200, json=queue.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpMCPTransport("https://mcp.example/rpc", client=client, danger=DangerLevel.SAFE)
    specs = run(transport.list_tools("srv"), loop)
    assert [s.name for s in specs] == ["search"]
    assert [m["method"] for m in captured] == [
        "initialize", "notifications/initialized", "tools/list",
    ]
    assert run(transport.call_tool("srv", "search", {"q": "x"}), loop) == "hit"
    assert captured[-1]["params"] == {"name": "search", "arguments": {"q": "x"}}
    assert run(transport.ping("srv"), loop) is True
    assert captured[-1]["method"] == "ping"

    run(transport.reconnect(), loop)
    queue.extend(_mcp_responses())
    assert run(transport.ping("srv"), loop) is True
    assert [m["method"] for m in captured].count("initialize") == 2

    run(transport.aclose(), loop)
    probe = run(client.get("https://mcp.example/rpc"), loop)
    assert probe.status_code == 500


def test_provider01_protocol_call_is_not_runnable_default_and_names_are_ordered() -> None:
    """输入：LLMProviderFactory.__call__ 以裸对象调用；LLMProviderCapability 注册 a/b 两端点。
    真实入口：LLMProviderFactory.__call__ / LLMProviderCapability.names。
    精确断言：协议默认实现抛 NotImplementedError（必须覆写）；names() 精确返回按注册顺序的 ('a','b')。
    失败判据：协议默认被静默实现或 names 顺序/内容不符。
    """
    from ports.adapters.provider import LLMProviderCapability, LLMProviderFactory

    with pytest.raises(NotImplementedError):
        LLMProviderFactory.__call__(
            SimpleNamespace(name="raw"), base_url="u", api_key="k", model="m"
        )

    class _LLM:
        def __init__(self, **config: Any) -> None:
            self.config = config

    cap = LLMProviderCapability(default_factory=_LLM)
    a = cap.register("a", base_url="a-url", api_key="k1", model="m1")
    b = cap.register("b", base_url="b-url", api_key="k2", model="m2")
    assert cap.names() == ("a", "b")
    assert a.config["base_url"] == "a-url"
    assert b.config["model"] == "m2"
