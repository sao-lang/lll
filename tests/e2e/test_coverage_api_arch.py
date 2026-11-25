"""补齐「当前 e2e 从未执行过」的公开 API：api.* 门面/装配层 + archs.* 配方架构。

纪律：每个用例走真实用户入口（Agent.say/run、AgentBuilder.build/runtime、compile→Program），
用可观察副作用（精确返回值、Effect 序列、事件/审计/快照状态）断言；不使用 isinstance /
hasattr / registry.has / 反射模板 / 恒真断言。异步路径统一用 conftest 的 loop + run。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from compose import (
    DangerLevel,
    LLMResponse,
    LifecyclePhase,
    Notice,
    PortCtx,
    ToolCall,
    ToolSpec,
    Usage,
    bind_input,
    current_input,
)
from runtime import (
    InMemoryEventStore,
    Invoke,
    Result,
    Session,
    Spawn,
    Suspend,
    Terminate,
)

from conftest import run
from tests.r12.conftest import drive


# --------------------------------------------------------------------------- #
# 测试用最小 Arch / 策略：只实现公开协议，compile 产出真实 Program，不用桩顶替入口
# --------------------------------------------------------------------------- #
def _report(name: str, ports: tuple[str, ...] = ()) -> Any:
    from archs.base import StaticCheckReport

    return StaticCheckReport(arch=name, version="1.0.0", ports_required=ports)


class _TermArch:
    """compile 直接 Terminate 的合法 Arch（供 build/静态检查用例）。"""

    name = "coverage-term"
    version = "1.0.0"

    def __init__(self, output: object) -> None:
        self.output = output

    def static_check(self) -> Any:
        return _report(self.name)

    def compile(self) -> Any:
        def program():
            yield Terminate(output=self.output)
            return None
        return program()


class _EchoArch:
    """从 bind_input 读语义输入，Invoke 自定义 port 后 Terminate 回注值。"""

    name = "coverage-echo"
    version = "1.0.0"

    def __init__(self, port: str = "echo") -> None:
        self.port = port

    def static_check(self) -> Any:
        return _report(self.name, (self.port,))

    def compile(self) -> Any:
        port = self.port

        def program():
            data = yield Invoke(port=port, payload={"value": current_input()})
            yield Terminate(output=data)
            return None
        return program()


class _SuspendArch:
    """yield Suspend → resume 回填 answer → Terminate（HITL 真闭环）。"""

    name = "coverage-suspend"
    version = "1.0.0"

    def static_check(self) -> Any:
        return _report(self.name)

    def compile(self) -> Any:
        def program():
            answer = yield Suspend(question="approve?", options=("yes", "no"))
            yield Terminate(output={"content": f"approved:{answer}"})
            return None
        return program()


class _MemoryCallArch:
    """Invoke(port='memory', op=read) → Terminate 回注，用于验注入策略真接入。"""

    name = "coverage-memory"
    version = "1.0.0"

    def static_check(self) -> Any:
        return _report(self.name, ("memory",))

    def compile(self) -> Any:
        def program():
            data = yield Invoke(port="memory", payload={
                "op": "read", "namespace": "ns", "key": "k",
            })
            yield Terminate(output=data)
            return None
        return program()


class _Named:
    """具名决策策略（Supervisor planner / 提示词型角色）。"""

    def __init__(self, name: str, prompt: str = "") -> None:
        self.name = name
        self.prompt = prompt


def _term_program(output: object) -> Any:
    def program():
        yield Terminate(output=output)
        return None
    return program


def _node_program(label: str, seen: list[str]) -> Any:
    def program():
        seen.append(label)
        yield Invoke(port="llm", payload={"node": label})
        yield Terminate(output={"node": label})
        return None
    return program


def _first_port(effect: Any) -> str:
    if isinstance(effect, (list, tuple)):
        effect = effect[0]
    return str(getattr(effect, "port", ""))


def _invoke_ports(effects: list[Any]) -> list[str]:
    ports: list[str] = []
    for effect in effects:
        if isinstance(effect, (list, tuple)):
            ports.extend(e.port for e in effect if isinstance(e, Invoke))
        elif isinstance(effect, Invoke):
            ports.append(effect.port)
    return ports


# =========================================================================== #
# api.builder（17）：构造 / profile / capability / 六项接线 / build / runtime
# =========================================================================== #
def test_bld01_init_and_build_produce_real_session() -> None:
    """输入合法 Arch→AgentBuilder.__init__/build 真产 Session→精确断言 session_id/version/工厂可驱动出 Terminate.output→失败判据：工厂不产出 'built' 或版本错位。"""
    from api.builder import AgentBuilder

    arch = _TermArch("built")
    session = AgentBuilder(arch).build()
    assert session.session_id == "agent"
    assert session.version == "coverage-term@1.0.0"
    effects, state = drive(session.program_factory, responder=lambda e, i: {})
    assert len(effects) == 1
    assert effects[0].output == "built"
    assert state.output == "built"


def test_bld02_profile_applied_and_unknown_rejected() -> None:
    """输入 'production'→AgentBuilder.profile/applied_profiles/capability_names 真装配→精确断言三份快照→失败判据：profile 未登记能力或未知名未 ValueError。"""
    from api.builder import AgentBuilder

    builder = AgentBuilder(_TermArch("x")).profile("production")
    assert builder.applied_profiles() == ("production",)
    assert builder.capability_names() == ("resilience", "rag", "security", "observability")
    with pytest.raises(ValueError, match="unknown profile"):
        AgentBuilder(_TermArch("x")).profile("no-such-profile")


def test_bld03_capability_installs_into_shared_registry() -> None:
    """输入自定义 Capability→AgentBuilder.capability/capability_names/registry 真调用 install→精确断言注册项身份→失败判据：resolve 不是注入的 provider。"""
    from api.builder import AgentBuilder

    provider = _Named("cov-provider")

    class _Cap:
        name = "coverage-cap"
        version = "1.0.0"
        requires: tuple[str, ...] = ()

        def install(self, container: object, reg: Any) -> None:
            reg.register("memory", provider)

    builder = AgentBuilder(_TermArch("x")).capability(_Cap())
    assert builder.capability_names() == ("coverage-cap",)
    assert builder.registry().resolve("memory") is provider


def test_bld04_approve_and_audit_injected_into_runtime() -> None:
    """输入审批/审计包装器→AgentBuilder.approve/audit/runtime 真装配→精确断言 rt 两侧身份与能力名→失败判据：包装器未进 AgentRuntime。"""
    from api.builder import AgentBuilder
    from api.wiring import ApprovingHumanPort, VerifyingAuditPort
    from harness.security import ConsoleHumanPort

    approval = ApprovingHumanPort(ConsoleHumanPort(answerer=lambda q, o: "yes"))
    audit = VerifyingAuditPort()
    builder = AgentBuilder(_TermArch("x")).approve(approval).audit(audit)
    assert builder.capability_names() == ("approval", "audit_verify")
    rt = builder.runtime(lambda e, c: Result.success(None))
    assert rt.approval is approval
    assert rt.audit is audit


def test_bld05_secrets_and_redact_real_masking() -> None:
    """输入含 sk 明文文本 + secret 引用→AgentBuilder.secrets/redact 真过护栏→精确断言 action/redacted/rule→失败判据：明文未被替换为 ***。"""
    from api.builder import AgentBuilder

    builder = AgentBuilder(_TermArch("x")).secrets(["sk-COV-SECRET"])
    verdict = builder.redact("token=sk-COV-SECRET", PortCtx(session_id="s", run_id="r"))
    assert verdict.action == "redact"
    assert verdict.redacted == "token=***"
    assert verdict.rule == "secret_filter"


def test_bld06_project_events_and_stream_bus_publish(loop: Any) -> None:
    """输入 EventEnvelope→AgentBuilder.project_events/stream_bus 真订阅三相位→emit 后 stream 精确读到 seq/type/data→失败判据：发布事件未进流。"""
    from api.builder import AgentBuilder
    from harness.observability import InMemoryEventStream

    stream = InMemoryEventStream()
    builder = AgentBuilder(_TermArch("x")).project_events(stream)
    bus = builder.stream_bus()
    bus.emit(Notice(
        phase=LifecyclePhase.AFTER_TOOL, session_id="s", port="tool",
        payload={"tag": "cov"}, seq=7,
    ))

    async def collect() -> list[Any]:
        return [e async for e in stream.subscribe("trace")]

    events = run(collect(), loop)  # type: ignore[arg-type]
    assert len(events) == 1
    assert events[0].seq == 7
    assert events[0].type == "after_tool"
    assert events[0].data["tag"] == "cov"
    assert events[0].data["seq_synthetic"] is False


def test_bld07_checkpoint_and_store_getter_write_snapshot(loop: Any) -> None:
    """输入 JsonSnapshot→AgentBuilder.checkpoint/checkpoint_store 真落共享 store→精确断言 load 回读与 manager 身份→失败判据：快照未写入或 manager 被替换。"""
    from api.builder import AgentBuilder
    from harness.durability import CheckpointManager

    store = InMemoryEventStore()
    manager = CheckpointManager(store=store)
    adapter = AgentBuilder(_TermArch("x")).checkpoint(manager).checkpoint_store()
    assert adapter.manager() is manager
    snapshot = {
        "session_id": "cov-cp", "version": "v1", "state": {},
        "events": [{"seq": 3}], "fingerprint": "fp",
    }
    run(adapter.save("cov-cp", snapshot), loop)  # type: ignore[arg-type]
    assert run(adapter.load("cov-cp"), loop) == snapshot  # type: ignore[arg-type]


def test_bld08_govern_denies_then_allows_build() -> None:
    """输入 Governance 熔断开关→AgentBuilder.govern/build 真判 gate→精确断言拒绝/放行→失败判据：熔断下仍产 Session 或恢复后仍拒绝。"""
    from api.builder import AgentBuilder
    from api.governance import Governance

    gov = Governance()
    gov.killswitch.enable("halt")
    builder = AgentBuilder(_TermArch("x")).govern(gov)
    with pytest.raises(ValueError, match="governance denied"):
        builder.build()
    gov.killswitch.disable()
    assert builder.build().session_id == "agent"


def test_bld09_runtime_real_run_with_all_wiring(loop: Any) -> None:
    """输入 echo Arch + 三接线→AgentBuilder.runtime/Runner 真跑→精确断言 output/同源 stream/store/事件→失败判据：output 非回注值或接线不同源。"""
    from api.builder import AgentBuilder
    from api.wiring import ApprovingHumanPort, VerifyingAuditPort
    from harness.observability import InMemoryEventStream
    from harness.security import ConsoleHumanPort

    stream = InMemoryEventStream()
    approval = ApprovingHumanPort(ConsoleHumanPort(answerer=lambda q, o: "yes"))
    audit = VerifyingAuditPort()
    builder = (AgentBuilder(_EchoArch())
               .approve(approval).audit(audit).project_events(stream).checkpoint())
    rt = builder.runtime(
        lambda e, c: Result.success({"content": "from-call"}), checkpoint_every=2)
    session = builder.build()
    result = run(rt.run(session), loop)  # type: ignore[arg-type]

    assert result.status == "done"
    assert result.output == {"content": "from-call"}
    assert rt.approval is approval and rt.audit is audit
    assert rt.stream is stream
    assert rt.checkpoint.inner is builder.checkpoint_store().inner
    assert len(session.events) >= 1

    async def collect() -> list[Any]:
        return [e async for e in stream.subscribe("trace")]

    assert len(run(collect(), loop)) >= 1  # type: ignore[arg-type]


# =========================================================================== #
# api.agent（9）：say/run/history/clear/stream/register_port/inject_strategy/resume
# =========================================================================== #
def test_agent01_say_run_history_clear_and_stream() -> None:
    """输入两句语义文本→Agent.say/run/register_port/history/clear_history/stream 真执行→精确断言回显/历史对/清空→失败判据：handler 未收到 Effect 或历史不配对。"""
    from api.agent import Agent
    from harness.observability import InMemoryEventStream

    seen: list[dict[str, Any]] = []

    async def handler(effect: Any, ctx: Any) -> Result:
        seen.append(dict(effect.payload))
        return Result.success({"content": f"echo:{effect.payload['value']}"})

    stream = InMemoryEventStream()
    agent = Agent(_EchoArch(), session_id="cov-agent", stream=stream)
    assert agent.stream is stream
    agent.register_port("echo", handler)

    assert agent.say("hello") == "echo:hello"
    assert seen == [{"value": "hello"}]
    assert agent.history == (
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "echo:hello"},
    )
    assert agent.run("world") == "echo:world"
    assert agent.history[-2:] == (
        {"role": "user", "content": "world"},
        {"role": "assistant", "content": "echo:world"},
    )
    agent.clear_history()
    assert agent.history == ()


def test_agent02_inject_strategy_really_consumed() -> None:
    """输入履约 MemoryPort 的策略→Agent.inject_strategy 签名校验并注册 handler→say 真调 read→精确断言输出与错误拒绝→失败判据：策略未接入或伪实现未被拒。"""
    from api.agent import Agent

    class _MemoryStrategy:
        name = "cov-memory"

        async def read(self, namespace: str, key: str, ctx: Any) -> str:
            return f"{namespace}:{key}:found"

        async def write(self, namespace: str, key: str, value: Any, ctx: Any) -> None:
            return None

        async def forget(self, namespace: str, key: str, ctx: Any) -> None:
            return None

    agent = Agent(_MemoryCallArch(), session_id="cov-strategy")
    assert agent.inject_strategy(_MemoryStrategy()) is agent
    assert agent.say("go") == "ns:k:found"
    with pytest.raises(TypeError, match="履约校验"):
        Agent(_MemoryCallArch()).inject_strategy(object())


def test_agent03_resume_sync_hitl_closes_loop() -> None:
    """输入挂起型 Arch→Agent.say 挂起 + Agent.resume 同步续跑→精确断言 wait_human/终态/历史→失败判据：say 给假答案或 resume 未回填 answer。"""
    from api.agent import Agent

    agent = Agent(_SuspendArch(), session_id="cov-hitl-sync")
    first = agent.say("start")
    assert first.startswith("[wait_human]")
    assert agent.last_run is not None and agent.last_run.status == "suspended"

    second = agent.resume("yes")
    assert second == "approved:yes"
    assert agent.last_run is not None and agent.last_run.status == "done"
    assert agent.history[-2:] == (
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "approved:yes"},
    )
    with pytest.raises(RuntimeError, match="无挂起会话"):
        agent.resume("again")


def test_agent04_aresume_async_hitl_closes_loop(loop: Any) -> None:
    """输入挂起型 Arch→run(agent.asay) 挂起 + run(agent.aresume) 异步续跑→精确断言回填 answer 的终态→失败判据：aresume 未续跑或答案未进入 output。"""
    from api.agent import Agent

    agent = Agent(_SuspendArch(), session_id="cov-hitl-async")
    first = run(agent.asay("begin"), loop)  # type: ignore[arg-type]
    assert first.startswith("[wait_human]")
    second = run(agent.aresume("yes"), loop)  # type: ignore[arg-type]
    assert second == "approved:yes"
    assert agent.last_run is not None and agent.last_run.status == "done"


# =========================================================================== #
# api.runtime（3） / api.wiring（3） / api.ports / api.profiles / api.stream
# =========================================================================== #
def test_runtime01_agent_runtime_resume_real_session(loop: Any) -> None:
    """输入挂起 Session→AgentRuntime.run/resume 真委托 Runner→精确断言 status/output/事件数→失败判据：resume 未从断点继续或无新事件。"""
    from api.runtime import assemble_runtime

    rt = assemble_runtime(lambda e, c: Result.success(None))
    session = Session(session_id="cov-rt", program_factory=_SuspendArch().compile)
    first = run(rt.run(session), loop)  # type: ignore[arg-type]
    assert first.status == "suspended"

    second = run(rt.resume(session, "go"), loop)  # type: ignore[arg-type]
    assert second.status == "done"
    assert second.output == {"content": "approved:go"}
    assert len(session.events) >= 2


def test_runtime02_model_gateway_shares_bus(loop: Any) -> None:
    """输入 LLM + Invoke→AgentRuntime.model_gateway 用同一 bus 构造网关→as_dispatch 真触发 after_model 通知→精确断言 output/Notice→失败判据：总线不同源导致 0 通知。"""
    from api.runtime import assemble_runtime

    class _LLM:
        name = "cov-llm"
        last_usage = None

        async def complete(self, req: Any, ctx: Any) -> LLMResponse:
            return LLMResponse(content="ok", tool_calls=[], usage=Usage(
                prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="cov")

        def stream(self, req: Any, ctx: Any) -> Any:
            raise NotImplementedError

    rt = assemble_runtime(lambda e, c: Result.success(None))
    seen: list[Notice] = []
    rt.bus.on(LifecyclePhase.AFTER_MODEL, lambda n: seen.append(n))
    gateway = rt.model_gateway(_LLM())
    effect = Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    result = run(gateway.as_dispatch()(effect, PortCtx(
        session_id="cov-gw", run_id="r")), loop)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.data["content"] == "ok"
    assert len(seen) == 1
    assert seen[0].phase is LifecyclePhase.AFTER_MODEL
    assert seen[0].port == "llm"
    assert seen[0].session_id == "cov-gw"


def test_runtime03_capability_tools_wires_audit(loop: Any) -> None:
    """输入审批/审计包装器→capability_tools 真装 ToolRegistry→LOW 工具真调用并写审计→精确断言 ToolResult/审计事件→失败判据：审计端口未接入或工具结果为空。"""
    from api.runtime import capability_tools
    from api.wiring import ApprovingHumanPort, VerifyingAuditPort
    from harness.security import ConsoleHumanPort

    approval = ApprovingHumanPort(ConsoleHumanPort(answerer=lambda q, o: "yes"))
    audit = VerifyingAuditPort()
    cap = capability_tools(approval, audit)
    registry = cap.registry()
    registry.register(ToolSpec(name="cov-low", danger=DangerLevel.LOW),
                      lambda **kw: {"v": kw["x"]})
    result = run(registry.call(
        ToolCall(call_id="c1", name="cov-low", args={"x": 9}, danger=DangerLevel.LOW),
        PortCtx(session_id="cov-captools", run_id="r"),
    ), loop)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.value == {"v": 9}
    events = audit.events()
    assert len(events) == 1
    assert events[0].action == "tool.low.call"


def test_wiring01_approving_ask_routes_by_danger(loop: Any) -> None:
    """输入 SAFE/CRITICAL 两档→ApprovingHumanPort.ask 真走审批流/inner→精确断言 'deny' 与 inner 回答→失败判据：SAFE 调了 inner 或 CRITICAL 未透传问题。"""
    from api.wiring import ApprovingHumanPort
    from harness.security import ConsoleHumanPort

    port = ApprovingHumanPort(ConsoleHumanPort(
        answerer=lambda q, o: f"answered:{q}"))
    ctx = PortCtx(session_id="s", run_id="r")
    assert run(port.ask("safe?", ["y", "n"], ctx, danger=DangerLevel.SAFE), loop) == "deny"  # type: ignore[arg-type]
    assert run(port.ask("critical?", ["y", "n"], ctx, danger=DangerLevel.CRITICAL), loop) == "answered:critical?"  # type: ignore[arg-type]


def test_wiring02_bind_sink_persists_audit_hash(loop: Any) -> None:
    """输入 R1AuditEventSink→VerifyingAuditPort.bind_sink/record 真落共享 store→精确断言快照事件与哈希→失败判据：审计未持久化或哈希不一致。"""
    from api.wiring import R1AuditEventSink, VerifyingAuditPort
    from harness.security import AuditLogger

    store = InMemoryEventStore()
    sink = R1AuditEventSink(store)
    inner = AuditLogger()
    audit = VerifyingAuditPort(inner)
    audit.bind_sink(sink)
    run(audit.record({"who": "u", "action": "cov.event", "when": 1},
                     PortCtx(session_id="cov-audit", run_id="r")), loop)  # type: ignore[arg-type]

    saved = run(store.load("__audit__:cov-audit"), loop)  # type: ignore[arg-type]
    assert saved["events"][0]["action"] == "cov.event"
    assert saved["events"][0]["hash"] == inner.events()[0].hash


def test_wiring03_redact_output_masks_declared_secret() -> None:
    """输入含明文 secret 的输出→redact_output 真过护栏→精确断言 redact/allow 两态→失败判据：声明引用后明文未脱敏。"""
    from api.wiring import redact_output

    ctx = PortCtx(session_id="s", run_id="r")
    verdict = redact_output("key=sk-COV-X", ctx, secret_refs=["sk-COV-X"])
    assert verdict.action == "redact"
    assert verdict.redacted == "key=***"
    assert verdict.rule == "secret_filter"
    assert redact_output("key=sk-COV-X", ctx, secret_refs=()).action == "allow"


def test_ports01_has_reflects_real_registration() -> None:
    """输入 memory/rag 两个 Port 名→PortRegistry.register/has 真改状态→精确断言 True/False 与 resolve 身份→失败判据：has 在注册前后无差别。"""
    from api.ports import PortRegistry

    registry = PortRegistry()
    adapter = _Named("cov-adapter")
    assert registry.has("memory") is False
    registry.register("memory", adapter)
    assert registry.has("memory") is True
    assert registry.resolve("memory") is adapter
    assert registry.has("rag") is False


def test_profiles01_profile_names_exact_snapshot() -> None:
    """输入无→profile_names 真读清单→精确断言三档顺序→失败判据：缺档或多档。"""
    from api.profiles import profile_names

    assert profile_names() == ("dev", "production", "regulated")


def test_stream01_project_readmodel_since_filter() -> None:
    """输入三事件两 topic→StreamGateway.publish/project 真投影→精确断言 count/seqs 与 since 过滤→失败判据：topic 串台或 since 未生效。"""
    from api.stream import StreamGateway
    from harness.observability import EventEnvelope, TimelineProjection

    gateway = StreamGateway()
    gateway.publish(EventEnvelope(seq=1, topic="t", ts=1.0, type="x", data={"n": 1}))
    gateway.publish(EventEnvelope(seq=2, topic="t", ts=2.0, type="x", data={"n": 2}))
    gateway.publish(EventEnvelope(seq=3, topic="other", ts=3.0, type="x", data={"n": 3}))

    model = gateway.project(TimelineProjection(), "t")
    assert model.name == "timeline"
    assert model.data == {"count": 2, "seqs": [1, 2]}
    assert gateway.project(TimelineProjection(), "t", since=1).data == {
        "count": 1, "seqs": [2],
    }


# =========================================================================== #
# archs.recipes（10 架构 compile 真驱动 Effect 序列/output）+ archs.base.ok
# =========================================================================== #
def test_arch01_tooluse_parallel_batches_and_output() -> None:
    """输入两个 ToolSpec→ToolUse.__init__/compile 真产 batch→drive 精确断言两 tool Effect 与回注 list→失败判据：未并行或顺序错。"""
    from archs.recipes import ToolUse

    arch = ToolUse([ToolSpec(name="a"), ToolSpec(name="b")])

    def responder(effect: Any, index: int) -> Any:
        assert isinstance(effect, list)
        return [{"tool": str(e.payload["call"]["name"])} for e in effect]

    effects, state = drive(arch.compile, responder=responder)
    assert _invoke_ports(effects) == ["tool", "tool"]
    assert state.output == [{"tool": "a"}, {"tool": "b"}]


def test_arch02_tooluse_serial_seq_and_static_check() -> None:
    """输入两个 ToolSpec(parallel=False)→ToolUse.compile/static_check 真执行→精确断言顺序输出与 E07/E09 报告→失败判据：串行结果丢项或错误码不符。"""
    from archs.recipes import ToolUse

    arch = ToolUse([ToolSpec(name="x"), ToolSpec(name="y")], parallel=False)

    def responder(effect: Any, index: int) -> Any:
        return {"tool": effect.payload["name"]}

    effects, state = drive(arch.compile, responder=responder)
    assert _invoke_ports(effects) == ["tool", "tool"]
    assert state.output == [{"tool": "x"}, {"tool": "y"}]

    assert ToolUse([]).static_check().ok is False
    assert {i.code for i in ToolUse([]).static_check().errors} == {"R12-E07"}
    assert {i.code for i in ToolUse([ToolSpec(name="")]).static_check().errors} == {"R12-E09"}
    assert ToolUse([ToolSpec(name="ok")]).static_check().ok is True


def test_arch03_react_loops_until_no_tool_calls() -> None:
    """输入 ToolSpec+llm+max_turns→ReAct.__init__/compile 真循环→精确断言 llm/tool/llm Effect 序列与最终 content→失败判据：未累积历史或提前收敛。"""
    from archs.recipes import ReAct

    arch = ReAct([ToolSpec(name="t")], llm="cov-model", max_turns=5)
    calls = {"n": 0}

    def responder(effect: Any, index: int) -> Any:
        port = _first_port(effect)
        if port == "llm":
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": "calling", "tool_calls": [{"name": "t", "args": {}}]}
            assert len(effect.payload["messages"]) == 2
            return {"content": "final", "tool_calls": []}
        return [{"value": 7}]

    effects, state = drive(arch.compile, responder=responder)
    assert _invoke_ports(effects) == ["llm", "tool", "llm"]
    assert calls["n"] == 2
    assert state.output == "final"


def test_arch04_react_static_check_budgets_and_ports() -> None:
    """输入合法/非法 ReAct 配置→static_check 真报告→精确断言 budgets_resolved 与错误码集合→失败判据：预算未登记或空工具未报 E07。"""
    from archs.recipes import ReAct

    report = ReAct([ToolSpec(name="t")], llm="m", max_turns=2).static_check()
    assert report.ok is True
    assert report.ports_required == ("llm", "tool")
    assert report.budgets_resolved == {"turns": 2}
    assert {i.code for i in ReAct([]).static_check().errors} == {"R12-E07", "R12-E11"}
    assert "R12-E17" in {i.code for i in ReAct(
        [ToolSpec(name="t")], max_turns=0).static_check().errors}


def test_arch05_planexecute_runs_real_planner_and_executor() -> None:
    """输入 's1;s2' + RulePlanner/Executor→PlanExecute.compile 真调策略并逐 Step 发 Effect→精确断言 tool 序列与 executor_output→失败判据：策略未被调用或步序错。"""
    from archs.recipes import PlanExecute
    from harness.cognition.services import RuleExecutor, RulePlanner

    arch = PlanExecute(RulePlanner(";"), RuleExecutor())

    def responder(effect: Any, index: int) -> Any:
        return {"ok": True, "at": index}

    with bind_input("s1;s2"):
        effects, state = drive(arch.compile, responder=responder)
    assert _invoke_ports(effects) == ["tool", "tool"]
    assert effects[0].payload["action"] == "s1"
    assert effects[1].payload["action"] == "s2"
    assert state.output == {"ok": True, "at": 1, "executor_output": "s2"}


def test_arch06_planexecute_static_check_reports_unfulfilled() -> None:
    """输入 None 角色与合法角色→PlanExecute.static_check 真报告→精确断言 ok 翻转与 E15→失败判据：缺角色仍判通过。"""
    from archs.recipes import PlanExecute
    from harness.cognition.services import RuleExecutor, RulePlanner

    bad = PlanExecute(None, None).static_check()
    assert bad.ok is False
    assert {i.code for i in bad.errors} == {"R12-E15"}
    good = PlanExecute(RulePlanner(), RuleExecutor()).static_check()
    assert good.ok is True
    assert good.ports_required == ("llm",)


def test_arch07_reflection_revises_until_passed() -> None:
    """输入 ThresholdReflector(PASS) + 两轮草稿→Reflection.compile 真反思→精确断言两 llm Effect 与最终 best→失败判据：未修订或 passes 判定失效。"""
    from archs.recipes import Reflection
    from harness.cognition.services import ThresholdReflector

    arch = Reflection(ThresholdReflector(min_len=100, pass_marker="PASS"),
                      max_rounds=3)

    def responder(effect: Any, index: int) -> Any:
        phase = effect.payload["messages"][0]["content"]
        return {"content": "PASS done" if phase == "revise" else "draft"}

    effects, state = drive(arch.compile, responder=responder)
    assert _invoke_ports(effects) == ["llm", "llm"]
    assert state.output == "PASS done"


def test_arch08_reflection_static_check_budgets_and_role() -> None:
    """输入 max_rounds=0 与合法配置→Reflection.static_check 真报告→精确断言 E17/budgets→失败判据：非法轮次未报或预算缺失。"""
    from archs.recipes import Reflection
    from harness.cognition.services import ThresholdReflector

    bad = Reflection(ThresholdReflector(), max_rounds=0).static_check()
    assert bad.ok is False
    assert "R12-E17" in {i.code for i in bad.errors}
    good = Reflection(ThresholdReflector(), max_rounds=2).static_check()
    assert good.ok is True
    assert good.budgets_resolved == {"rounds": 2}


def test_arch09_router_selects_arm_and_default() -> None:
    """输入 IntentResult(高/低置信)→Router.__init__/compile/branch_value 真路由→精确断言命中 arm 与 default 输出→失败判据：置信阈值未生效。"""
    from archs.recipes import Router
    from archs.base import IntentResult

    arms = {"a": _term_program("a-out"), "b": _TermArch("b-out")}
    high = Router(lambda t: IntentResult(label="b", confidence=0.9), arms, default="a")
    _, high_state = drive(high.compile, responder=lambda e, i: {})
    assert high_state.output == "b-out"

    low = Router(lambda t: IntentResult(label="b", confidence=0.1), arms,
                 default="a", min_confidence=0.5)
    _, low_state = drive(low.compile, responder=lambda e, i: {})
    assert low_state.output == "a-out"


def test_arch10_router_static_check_flags_arms_and_default() -> None:
    """输入空 arms / 非法 default→Router.static_check 真报告→精确断言 E07/E08→失败判据：缺 arm 或未注册 default 被放过。"""
    from archs.recipes import Router

    assert "R12-E07" in {i.code for i in Router(None, {}).static_check().errors}
    bad_default = Router(None, {"a": _term_program("a")}, default="z").static_check()
    assert bad_default.ok is False
    assert "R12-E08" in {i.code for i in bad_default.errors}


def test_arch11_routerskill_loads_skill_body_then_downstream() -> None:
    """输入 SkillSpec(downstream=Arch)→RouterSkill.__init__/compile 真发 skill Effect 并跑下游→精确断言 payload/output→失败判据：正文未加载或下游未执行。"""
    from archs.recipes import RouterSkill
    from archs.base import IntentResult

    class _Skill:
        name = "skill-a"
        downstream = _TermArch("down-out")

    arch = RouterSkill(lambda t: IntentResult(label="sk", confidence=0.9),
                       {"sk": _Skill()}, default="sk")
    effects, state = drive(arch.compile, responder=lambda e, i: {"loaded": True})
    assert _invoke_ports(effects) == ["skill"]
    assert effects[0].payload == {"name": "skill-a", "part": "body"}
    assert state.output == "down-out"


def test_arch12_supervisor_spawns_children_and_joins() -> None:
    """输入拆任务提示 + child_spec.build→Supervisor.__init__/compile 真扇出→精确断言两个 Spawn 与 majority 合并输出→失败判据：子代理未派生或 join 丢失。"""
    from archs.recipes import Supervisor

    class _Spec:
        name = "spec"

        def build(self, task: object) -> Any:
            def child():
                yield Terminate(output={"task": task})
                return None
            return child

    arch = Supervisor(_Named("p", "split"), _Spec())

    def responder(effect: Any, index: int) -> Any:
        if isinstance(effect, (list, tuple)):
            return ["t1", "t2"]
        return {"tasks": ["t1", "t2"]}

    effects, state = drive(arch.compile, responder=responder)
    assert len([e for e in effects if isinstance(e, Spawn)]) == 2
    assert state.output == "t1"

    report = arch.static_check()
    assert report.ok is True
    assert report.budgets_resolved == {"parallel_children": 8}
    assert "R12-E15" in {i.code for i in Supervisor(
        _Named(""), _Spec()).static_check().errors}


def test_arch13_handoff_relays_roles_in_order() -> None:
    """输入三个 plain 角色→Handoff.__init__/compile 真逐跳接力→精确断言 name 序列与末位 output→失败判据：跳序错或漏角色。"""
    from archs.recipes import Handoff

    arch = Handoff({"a": object(), "b": object(), "c": object()})
    seen: list[str] = []

    def responder(effect: Any, index: int) -> Any:
        seen.append(effect.payload["name"])
        return {"role": effect.payload["name"]}

    effects, state = drive(arch.compile, responder=responder)
    assert seen == ["a", "b", "c"]
    assert _invoke_ports(effects) == ["llm", "llm", "llm"]
    assert state.output == {"role": "c"}

    bad = Handoff({}, max_hops=0).static_check()
    assert bad.ok is False
    assert {i.code for i in bad.errors} == {"R12-E07", "R12-E17"}


def test_arch14_blackboard_first_writer_wins() -> None:
    """输入两个写共享键的 agent→Blackboard.__init__/compile 真并行+多轮→精确断言 4 个 Spawn、首写胜出与轮次→失败判据：共享键被覆盖或轮次不递增。"""
    from archs.recipes import Blackboard

    def agent_a():
        yield Invoke(port="llm", payload={"who": "a"})
        yield Terminate(output={"writes": {"k": "from-a"}})
        return None

    def agent_b():
        yield Invoke(port="llm", payload={"who": "b"})
        yield Terminate(output={"writes": {"k": "from-b", "other": 1}})
        return None

    arch = Blackboard([agent_a, agent_b], rounds=2)

    def responder(effect: Any, index: int) -> Any:
        if isinstance(effect, (list, tuple)):
            return [{"writes": {"k": "from-a"}},
                    {"writes": {"k": "from-b", "other": 1}}]
        return {}

    effects, state = drive(arch.compile, responder=responder)
    assert len([e for e in effects if isinstance(e, Spawn)]) == 4
    assert state.output["k"] == "from-a"
    assert state.output["other"] == 1
    assert state.output["x-arch.bb.round"] == 2

    bad = Blackboard([], rounds=0).static_check()
    assert bad.ok is False
    assert {i.code for i in bad.errors} == {"R12-E07", "R12-E17"}


def test_arch15_graph_cond_branch_traverses_to_exit() -> None:
    """输入 start/left/right 三节点+cond 边→Graph.__init__/node/edge/exit/compile 真行走→精确断言访问序与 exit output→失败判据：cond 边未选中或走到 right。"""
    from archs.recipes import Graph

    seen: list[str] = []
    arch = (Graph()
            .node("start", _node_program("start", seen))
            .node("left", _node_program("left", seen))
            .node("right", _node_program("right", seen))
            .edge("start", "left", cond=lambda s: "left")
            .edge("start", "right")
            .exit("left"))
    report = arch.static_check()
    assert report.ok is True
    assert report.ports_required == ("llm",)

    effects, state = drive(arch.compile, responder=lambda e, i: {})
    assert seen == ["start", "left"]
    assert _invoke_ports(effects) == ["llm", "llm"]
    assert state.output == {"node": "left"}


def test_arch16_graph_static_check_detects_dangling_edge() -> None:
    """输入悬空边图→Graph.edge/exit/static_check 真检测→精确断言 E01 与 ok=False→失败判据：悬空边被判通过。"""
    from archs.recipes import Graph

    graph = Graph().node("a", _term_program(None)).edge("a", "ghost").exit("a")
    report = graph.static_check()
    assert report.ok is False
    assert {i.code for i in report.errors} == {"R12-E01"}


def test_arch17_static_check_report_ok_both_states() -> None:
    """输入空/非空 errors→StaticCheckReport.ok 属性真判定→精确断言 True/False→失败判据：ok 不随 errors 变化。"""
    from archs.base import StaticCheckIssue, StaticCheckReport

    assert StaticCheckReport().ok is True
    bad = StaticCheckReport(errors=(StaticCheckIssue("X", "bad"),))
    assert bad.ok is False
