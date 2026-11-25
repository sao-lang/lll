"""补齐「当前 e2e 从未执行过」的公开 API：runtime 内核 + 可观测/评测/认知/安全/技能/提示。

纪律：每个用例走真实入口（Runner / Interpreter / CheckpointManager / Trace / RuleEval /
intent_as_program / SecurityCtx.from_invocation ...），断言精确返回值或可观察副作用；
不使用 isinstance / hasattr / registry.has / 反射模板 / 恒真断言。异步路径统一用
conftest 的 loop + run，每个用例独立实例。
"""
from __future__ import annotations

import json
from string import Template
from types import SimpleNamespace
from typing import Any

import pytest

from compose import DangerLevel, Invocation, PortCtx, SkillManifest
from harness.cognition import (
    CascadeIntentStrategy,
    InMemoryIntentSampleStore,
    IntentResult,
    RegexKeywordMatcher,
    RuleExecutor,
    RulePlanner,
    RuleRePlanner,
    Step,
    StepResult,
    intent_as_program,
)
from harness.durability import CheckpointManager
from harness.eval import EvalCase, GoldenJudge, RuleEval, Score, SuiteReport
from harness.observability import (
    EventEnvelope,
    InMemoryEventStream,
    NoopTelemetry,
    Projection,
    ReadModel,
    TimelineProjection,
    Trace,
)
from harness.prompt import PromptCapability, PromptTemplate
from harness.security import (
    ApprovalFlow,
    SafetyVerdict,
    SecurityCapability,
    SecurityCtx,
    redact_secret,
)
from harness.skills import FileSkillProvider
from runtime import (
    DONE,
    AgentState,
    CancellationToken,
    InMemoryEventStore,
    Invoke,
    Message,
    PortError,
    Result,
    Runner,
    Session,
    Suspend,
    Terminate,
)
from runtime.errors import PortMissing
from runtime.interpreter import InterpreterImpl
from runtime.runner import _CancellationHolder

from conftest import run


# =========================================================================== #
# runtime.runner（5）：_CancellationHolder.cancel / Runner.cancel / resume / replay / load
# =========================================================================== #
def test_runner_holder_cancel_records_reason_in_place() -> None:
    """输入取消原因→_CancellationHolder.cancel 原地置位→断言布尔与原因精确→失败判据：字段未按原因更新。"""
    holder = _CancellationHolder()
    holder.cancel("holder-stop")
    assert (holder.cancelled, holder.reason) == (True, "holder-stop")


def test_runner_cancel_stops_next_step_and_skips_effect(loop: Any) -> None:
    """输入先跑一步再 cancel→Runner.step 检查 holder 写 cancelled→断言原因/空 Effect/调用计数→失败判据：取消后仍调用 Port。"""
    seen: list[str] = []

    def program():
        for _ in range(4):
            yield Invoke(port="llm", payload={})
        yield Terminate(output="unreachable")

    async def call(effect: Any, ctx: Any) -> Result:
        seen.append(effect.port)
        return Result.success("ok")

    runner = Runner(call=call, cancellation=CancellationToken())
    session = Session(session_id="s-cancel", program_factory=lambda: program())
    first = run(runner.step(session), loop)
    assert (first.status, seen) == ("running", ["llm"])

    runner.cancel("user halted")
    second = run(runner.step(session), loop)
    assert (second.status, second.reason, second.effects) == ("cancelled", "user halted", ())
    assert seen == ["llm"], "取消后不得再执行任何 Effect"
    final = runner._result(session)
    assert (final.status, final.reason, session.events[-1].reason) == (
        "cancelled", "user halted", "user halted",
    )


def test_runner_resume_feeds_answer_back_to_suspend_point(loop: Any) -> None:
    """输入 Suspend→Runner.run/resume 真闭环→断言挂起态/回填答案/终态输出→失败判据：答案未进入 Program 或 seen 不符。"""
    seen: list[str] = []

    def program():
        answer = yield Suspend(question="approve?", options=("yes", "no"))
        seen.append(answer)
        yield Terminate(output=f"chosen:{answer}")
        return None

    runner = Runner(call=lambda e, c: Result.success(None))
    session = Session(session_id="s-resume", program_factory=lambda: program())
    suspended = run(runner.run(session), loop)
    assert (suspended.status, suspended.output) == ("suspended", None)

    resumed = run(runner.resume(session, "yes"), loop)
    assert (resumed.status, resumed.output, seen) == ("done", "chosen:yes", ["yes"])
    assert [e.status for e in session.events] == ["suspended", "done"]


def test_runner_replay_rebuilds_terminal_state_from_events(loop: Any) -> None:
    """输入已跑完会话→Runner.replay 逐步 fold 事件→断言输出/预算/消息/原因全等终态→失败判据：重放丢字段。"""
    async def call(effect: Any, ctx: Any) -> Result:
        return Result.success({"content": "answer"})

    def program():
        resp = yield Invoke(port="llm", payload={})
        yield Terminate(output=resp["content"])
        return None

    runner = Runner(call=call)
    initial = AgentState()
    session = Session(session_id="s-replay", program_factory=lambda: program())
    result = run(runner.run(session), loop)
    replayed = runner.replay(session, initial)
    assert (
        replayed.output,
        replayed.budgets,
        replayed.messages,
        replayed.last_reason,
    ) == (
        result.output,
        result.state.budgets,
        result.state.messages,
        result.state.last_reason,
    )
    assert (result.output, result.state.last_reason) == ("answer", "terminated")


def test_runner_load_restores_checkpoint_and_rebuilds_program(loop: Any) -> None:
    """输入已落盘会话→Runner.load 从 store 恢复→断言 Session 字段/事件/工厂再驱动→失败判据：快照读回字段错位或缺快照未报错。"""
    store = InMemoryEventStore()

    def program():
        yield Invoke(port="llm", payload={})
        yield Terminate(output="loaded")
        return None

    runner = Runner(call=lambda e, c: Result.success(None), store=store)
    session = Session(session_id="s-load", program_factory=lambda: program())
    assert run(runner.run(session), loop).status == "done"
    snapshot = run(store.load("s-load"), loop)
    assert (snapshot["session_id"], snapshot["version"]) == ("s-load", session.version)

    fresh = Runner(call=lambda e, c: Result.success(None), store=store)
    restored = run(fresh.load("s-load", lambda: program()), loop)
    assert (restored.session_id, restored.version, restored.state.output) == (
        "s-load", session.version, "loaded",
    )
    assert [e.status for e in restored.events] == [e.status for e in session.events]
    first = next(restored.program_factory())
    assert (first.port, dict(first.payload)) == ("llm", {})

    with pytest.raises(PortMissing):
        run(fresh.load("never-saved", lambda: program()), loop)


# =========================================================================== #
# runtime.fold（2）：state_set / state_append_message
# =========================================================================== #
def test_state_set_writes_scratch_and_managed_keys_immutably() -> None:
    """输入空 State 与键→state_set 真写入→断言用户键与托管键精确且原对象不变→失败判据：写入就地改了原 State。"""
    from runtime.fold import state_set

    original = AgentState()
    scratch = state_set(original, "phase", "build")
    assert (original.get("phase"), scratch.get("phase")) == (None, "build")
    managed = state_set(scratch, "output", "done")
    assert (managed.output, scratch.output, scratch.get("phase")) == ("done", None, "build")


def test_state_append_message_appends_exact_tuple_and_keeps_original() -> None:
    """输入 State 与两条消息→state_append_message 逐条追加→断言三次 messages 元组精确→失败判据：原 State 被就地改写。"""
    from runtime.fold import state_append_message

    original = AgentState()
    first = Message(role="user", content="hi")
    second = Message(role="assistant", content="hello")
    one = state_append_message(original, first)
    two = state_append_message(one, second)
    assert (original.messages, one.messages, two.messages) == ((), (first,), (first, second))


# =========================================================================== #
# runtime.types（2）：Result.failure / CancellationToken.cancel
# =========================================================================== #
def test_result_failure_builds_exact_failure_envelope() -> None:
    """输入码/消息/retryable→Result.failure 真构造→断言整对象等于预期 PortError 信封→失败判据：错误码或标志丢失。"""
    failed = Result.failure("TOOL_TIMEOUT", "timed out", retryable=True)
    assert failed == Result(
        ok=False,
        data=None,
        usage=None,
        error=PortError(code="TOOL_TIMEOUT", message="timed out", retryable=True),
    )


def test_cancel_token_returns_new_token_and_keeps_original() -> None:
    """输入原因→CancellationToken.cancel 返回新令牌→断言新/旧令牌字段精确→失败判据：原令牌被改写。"""
    token = CancellationToken()
    cancelled = token.cancel("halt")
    assert (cancelled.cancelled, cancelled.reason) == (True, "halt")
    assert (token.cancelled, token.reason) == (False, "")


# =========================================================================== #
# runtime.interpreter（1）：InterpreterImpl.started
# =========================================================================== #
def test_interpreter_started_flips_only_after_first_advance() -> None:
    """输入 generator→InterpreterImpl.send 推进→断言 started 首推前 False/后 True、DONE 终止→失败判据：started 未随推进变化。"""
    def program():
        yield Invoke(port="llm", payload={})
        yield Terminate(output="x")
        return None

    interpreter = InterpreterImpl(program())
    assert interpreter.started is False
    batch = interpreter.send(None)
    assert (interpreter.started, [e.port for e in batch.effects]) == (True, ["llm"])
    terminal = interpreter.send(None)
    assert terminal.effects[0].output == "x"
    assert interpreter.send(None) is DONE


# =========================================================================== #
# harness.durability.checkpoint（2）：snapshot / save
# =========================================================================== #
def test_checkpoint_snapshot_carries_state_events_and_exact_fingerprint() -> None:
    """输入 State 与两个事件→snapshot 产具名 DTO→断言字段与精确 SHA256 指纹→失败判据：指纹或字段不符。"""
    manager = CheckpointManager(version="2.0")
    state = AgentState(scratch={"k": 1})
    events = ("e1", "e2")
    snap = manager.snapshot("s-cp", state, events)
    assert (snap.session_id, snap.version, snap.state, snap.events) == (
        "s-cp", "2.0", state, events,
    )
    assert snap.fingerprint == "1058410a0a0ae30d"


def test_checkpoint_save_persists_json_snapshot_through_store(loop: Any) -> None:
    """输入快照→save 经 R1 EventStore 落盘→断言读回 session/version/state/events/指纹→失败判据：存储内容缺失或错位。"""
    store = InMemoryEventStore()
    manager = CheckpointManager(store=store, version="3.0")
    snap = manager.snapshot("s-save", AgentState(scratch={"k": "v"}), ("e1",))

    run(manager.save(snap), loop)
    data = run(store.load("s-save"), loop)
    assert (data["session_id"], data["version"], data["fingerprint"]) == (
        "s-save", "3.0", snap.fingerprint,
    )
    assert (data["state"]["scratch"], data["events"]) == ({"k": "v"}, ["e1"])


# =========================================================================== #
# harness.observability.stream（3）：dropped_count / Projection.project / TimelineProjection.project
# =========================================================================== #
def test_event_stream_dropped_count_tracks_real_backpressure_drops(loop: Any) -> None:
    """输入容量 2 与 4 次发布→dropped_count 与丢弃主题→断言计数/seq/载荷精确→失败判据：丢事件未计数或保留集错误。"""
    stream = InMemoryEventStream(capacity=2)
    for i in (1, 2, 3, 4):
        stream.publish(EventEnvelope(
            seq=i, topic="metric.x", ts=float(i), type="m", data={"i": i},
        ))

    async def drain(topic: str) -> list[Any]:
        return [e async for e in stream.subscribe(topic)]

    kept = run(drain("metric.x"), loop)
    dropped = run(drain("trace.event_dropped"), loop)
    assert stream.dropped_count() == 2
    assert [e.seq for e in kept] == [3, 4]
    assert [e.data["dropped"] for e in dropped] == [1, 1]


def test_base_projection_projects_exact_count_readmodel() -> None:
    """输入三个事件→Projection.project 重建读模型→断言精确 ReadModel→失败判据：计数或 name 不符。"""
    events = [
        EventEnvelope(seq=i, topic="t", ts=0.0, type="x", data=None) for i in (1, 2, 3)
    ]
    model = Projection().project(events)
    assert model == ReadModel(name="projection", data={"count": 3})


def test_timeline_projection_projects_exact_seqs_readmodel() -> None:
    """输入非连续序号事件→TimelineProjection.project→断言精确 seqs 与计数→失败判据：序号顺序或计数不符。"""
    events = [
        EventEnvelope(seq=i, topic="trace.span", ts=0.0, type="span", data=None)
        for i in (7, 9, 11)
    ]
    model = TimelineProjection().project(events)
    assert model == ReadModel(name="timeline", data={"count": 3, "seqs": [7, 9, 11]})


# =========================================================================== #
# harness.observability.telemetry（2）：Trace.record_metric / Trace.record_log
# =========================================================================== #
def test_trace_record_metric_writes_exact_metric_tuple() -> None:
    """输入名称/数值/tags→Trace.record_metric 真入 NoopTelemetry→断言指标元组精确→失败判据：tags 丢失或值不符。"""
    telemetry = NoopTelemetry()
    trace = Trace(trace_id="t-metric", telemetry=telemetry)
    trace.record_metric("latency_ms", 12.5, tags={"model": "m1"})
    assert telemetry.metrics() == (("latency_ms", 12.5, {"model": "m1"}),)


def test_trace_record_log_writes_exact_log_tuple() -> None:
    """输入结构化事件→Trace.record_log 真入 NoopTelemetry→断言日志元组精确→失败判据：日志内容不符。"""
    telemetry = NoopTelemetry()
    trace = Trace(trace_id="t-log", telemetry=telemetry)
    trace.record_log({"event": "tool.done", "ok": True})
    assert telemetry.logs() == ({"event": "tool.done", "ok": True},)


# =========================================================================== #
# harness.eval.rules（3）：add_case / regression / GoldenJudge.score
# =========================================================================== #
def test_rule_eval_add_case_changes_scoring_exactly(loop: Any) -> None:
    """输入注册前后两次评分→add_case 改 golden 集→断言 0.0→1.0→0.0 精确序列→失败判据：注册未生效。"""
    ctx = PortCtx(session_id="s-eval", run_id="r-eval")
    evaluator = RuleEval()
    assert run(evaluator.score("c1", "a", ctx), loop) == 0.0
    evaluator.add_case(EvalCase(case_id="c1", input="q", expected="a"))
    assert run(evaluator.score("c1", "a", ctx), loop) == 1.0
    assert run(evaluator.score("c1", "b", ctx), loop) == 0.0


def test_rule_eval_regression_reports_exact_counts(loop: Any) -> None:
    """输入三例（通过/失败/未采集）→regression 真评分→断言 SuiteReport 精确→失败判据：未采集用例被误算通过。"""
    ctx = PortCtx(session_id="s-eval", run_id="r-eval")
    evaluator = RuleEval()
    evaluator.add_case(EvalCase(case_id="pass", input="q", expected="ok", actual="ok"))
    evaluator.add_case(EvalCase(case_id="fail", input="q", expected="ok", actual="bad"))
    evaluator.add_case(EvalCase(case_id="uncollected", input="q", expected="ok"))
    report = run(evaluator.regression("suite-alpha", ctx), loop)
    assert report == SuiteReport(suite="suite-alpha", total=3, passed=1, average=1 / 3)


def test_golden_judge_score_exact_and_partial_trajectory(loop: Any) -> None:
    """输入轨迹用例→GoldenJudge.score 真比对→断言全等 1.0/部分 0.5 及 reason→失败判据：交集并集口径不符。"""
    ctx = PortCtx(session_id="s-eval", run_id="r-eval")
    case = EvalCase(case_id="c", input="q", expected_trajectory=("a", "b"))
    judge = GoldenJudge()
    exact = run(judge.score(case, ["a", "b"], ctx), loop)
    partial = run(judge.score(case, ["a"], ctx), loop)
    assert (exact.value, exact.reason) == (1.0, "exact_trajectory")
    assert (partial.value, partial.reason) == (0.5, "partial_trajectory")
    assert exact == Score(value=1.0, reason="exact_trajectory")


# =========================================================================== #
# harness.cognition.cascade（7）
# =========================================================================== #
def test_sample_store_similarity_is_exact_bigram_jaccard() -> None:
    """输入四组文本→InMemoryIntentSampleStore.similarity 纯函数→断言精确浮点值→失败判据：大小写/空串口径不符。"""
    similarity = InMemoryIntentSampleStore.similarity
    assert (
        similarity("word", "world"),
        similarity("ab", "ab"),
        similarity("ab", "cd"),
        similarity("", ""),
    ) == (0.4, 1.0, 0.0, 0.0)


def test_cascade_build_short_circuits_at_l1_with_exact_output() -> None:
    """输入关键词命中文本→CascadeIntentStrategy.build 真产 Program→断言 Terminate 载荷精确且无后续 Effect→失败判据：短路失效。"""
    strategy = CascadeIntentStrategy(
        keyword=RegexKeywordMatcher({"hello": "greeting"}),
    )
    gen = strategy.build("hello there")
    effect = next(gen)
    assert effect.output == {
        "label": "greeting",
        "confidence": 0.9,
        "gaps": [],
        "trace": ("L1[keyword]:greeting:0.90",),
        "hit_stage": "L1",
    }
    with pytest.raises(StopIteration):
        next(gen)


def test_intent_as_program_builds_real_program_from_strategy() -> None:
    """输入带 build 的级联策略→intent_as_program 产 ProgramFn→断言驱动出的输出精确→失败判据：桥梁未调用 build。"""
    strategy = CascadeIntentStrategy(keyword=RegexKeywordMatcher({"urgent": "escalate"}))
    program_fn = intent_as_program(strategy, "urgent please")
    effect = next(program_fn())
    assert (effect.output["label"], effect.output["hit_stage"]) == ("escalate", "L1")
    assert effect.output["trace"] == ("L1[keyword]:escalate:0.90",)


def test_intent_as_program_from_build_delegates_once() -> None:
    """输入自定义 build 策略→from_build 真调用一次→断言输出与调用计数精确→失败判据：build 未调用或调用多次。"""
    class Built:
        calls = 0

        def build(self, text: str):
            type(self).calls += 1

            def gen():
                yield Terminate(output={"label": f"built:{text}", "hit_stage": "custom"})
                return None

            return gen()

    program_fn = intent_as_program(Built(), "req-1")
    effect = next(program_fn())
    assert effect.output == {"label": "built:req-1", "hit_stage": "custom"}
    assert Built.calls == 1


def test_intent_as_program_adapted_runs_async_recognize() -> None:
    """输入仅 recognize 的策略→adapted 真 await 并归一→断言输出全字段精确→失败判据：async 结果未进入 Terminate。"""
    class Recognizer:
        name = "recognizer"

        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="adapted-hit", confidence=0.85, gaps=("slot",))

    effect = next(intent_as_program(Recognizer(), "anything")())
    assert effect.output == {
        "label": "adapted-hit",
        "confidence": 0.85,
        "gaps": ["slot"],
        "trace": ("strategy:adapted",),
        "hit_stage": "strategy",
    }


def test_intent_as_program_callable_adapter_normalizes_result() -> None:
    """输入同步/异步 Callable→callable_adapter 真调用归一→断言两条输出精确→失败判据：字典/协程未归一为意图载荷。"""
    def classifier(text: str) -> dict[str, Any]:
        return {"label": "callable-hit", "confidence": 0.77}

    effect = next(intent_as_program(classifier, "x")())
    assert effect.output == {
        "label": "callable-hit",
        "confidence": 0.77,
        "gaps": [],
        "trace": ("strategy:callable",),
        "hit_stage": "strategy",
    }

    async def async_classifier(text: str) -> IntentResult:
        return IntentResult(label="async-callable", confidence=0.6)

    other = next(intent_as_program(async_classifier, "x")())
    assert (other.output["label"], other.output["confidence"]) == ("async-callable", 0.6)


def test_intent_as_program_fallback_for_non_strategy() -> None:
    """输入 None→fallback 产 unknown→断言输出全字段精确→失败判据：兜底 label 或 trace 不符。"""
    effect = next(intent_as_program(None, "x")())
    assert effect.output == {
        "label": "unknown",
        "confidence": 0.0,
        "gaps": [],
        "trace": (),
        "hit_stage": "default",
    }


# =========================================================================== #
# harness.cognition.services（plan / __init__ / execute / replan）
# =========================================================================== #
def test_rule_planner_plan_splits_exact_steps() -> None:
    """输入目标串与两种分隔符→RulePlanner.plan 真切分→断言 Step 元组精确→失败判据：空段未跳过或 step_id 错位。"""
    assert RulePlanner().plan("a; b;;c ") == (
        Step(action="a", step_id="s0"),
        Step(action="b", step_id="s1"),
        Step(action="c", step_id="s2"),
    )
    assert RulePlanner("|").plan("x|y") == (
        Step(action="x", step_id="s0"),
        Step(action="y", step_id="s1"),
    )


def test_rule_executor_execute_returns_exact_step_result() -> None:
    """输入具名 Step→RuleExecutor.execute 真产出→断言 StepResult 精确→失败判据：action 未回显或 ok 标志丢失。"""
    result = RuleExecutor().execute(Step(action="scan", check="nonempty", step_id="s2"))
    assert result == StepResult(step_id="s2", output="scan", ok=True)


def test_rule_replanner_replan_prefixes_failure_exactly() -> None:
    """输入步骤与失败原因→RuleRePlanner.replan 真重排→断言首步标注/剩余步保留/空步兜底精确→失败判据：原因未写入首步。"""
    steps = (Step(action="a", check="c0", step_id="s0"), Step(action="b", step_id="s1"))
    revised = RuleRePlanner().replan("a;b", steps, ("first", "tool_timeout"))
    assert revised == (
        Step(action="a [tool_timeout]", check="c0", step_id="s0"),
        Step(action="b", step_id="s1"),
    )
    assert RuleRePlanner().replan("only-goal", (), ()) == (
        Step(action="only-goal", step_id="s0"),
    )


# =========================================================================== #
# harness.security（approval / capability / guard / types）
# =========================================================================== #
def test_approval_flow_decide_timeout_always_denies() -> None:
    """输入不同 timeout 配置→decide_timeout 真裁决→断言恒为 deny 精确字符串→失败判据：超时被静默放行。"""
    assert ApprovalFlow(timeout=0).decide_timeout() == "deny"
    assert ApprovalFlow(timeout=300).decide_timeout() == "deny"


def test_security_capability_build_yields_sandbox_invoke_then_terminate() -> None:
    """输入空→SecurityCapability.build 产 Program→断言首 Effect port/payload 与终 Effect output 精确→失败判据：安全层未落到 sandbox port。"""
    gen = SecurityCapability().build()
    first = next(gen)
    assert (first.port, dict(first.payload)) == ("sandbox", {"layer": "security"})
    terminal = gen.send(None)
    assert (terminal.output, terminal.reason) == (None, "")
    with pytest.raises(StopIteration):
        next(gen)


def test_redact_secret_replaces_every_occurrence_exactly() -> None:
    """输入含两处 secret 的文本→redact_secret 真脱敏→断言 SafetyVerdict 精确→失败判据：出现次数或规则名不符。"""
    verdict = redact_secret("token=sk-abc and sk-abc", ("sk-abc",))
    assert verdict == SafetyVerdict(
        action="redact", redacted="token=*** and ***", rule="secret_filter",
    )


def test_security_ctx_from_invocation_projects_exact_fields() -> None:
    """输入 Invocation 与覆写参数→SecurityCtx.from_invocation 真投影→断言具名字段精确且含 fail-closed 回退→失败判据：source/danger 未归一。"""
    inv = Invocation(
        effect=Invoke(port="tool", payload={}),
        ctx=SimpleNamespace(trace_span="root-1"),
        session=SimpleNamespace(session_id="s-sec", run_id="r-sec"),
        meta={"danger": "HIGH", "origin": "unit"},
    )
    ctx = SecurityCtx.from_invocation(inv, source="rag", direction="output", trust=True)
    assert (
        ctx.session_id,
        ctx.source,
        ctx.direction,
        ctx.danger,
        ctx.trust,
        ctx.meta,
    ) == ("s-sec", "rag", "output", DangerLevel.HIGH, True,
          {"danger": "HIGH", "origin": "unit"})

    bad = Invocation(
        effect=Invoke(port="tool", payload={}),
        ctx=SimpleNamespace(),
        session=SimpleNamespace(session_id="s-bad"),
        meta={"danger": "NOT_A_LEVEL"},
    )
    closed = SecurityCtx.from_invocation(bad, source="unknown", direction="sideways")
    assert (
        closed.source, closed.direction, closed.danger, closed.trust, closed.meta
    ) == ("tool", "input", DangerLevel.CRITICAL, False, {"danger": "NOT_A_LEVEL"})


# =========================================================================== #
# harness.skills.provider（catalog）与 harness.prompt（register）
# =========================================================================== #
def test_file_skill_provider_catalog_reads_disk_without_caching(tmp_path: Any) -> None:
    """输入磁盘技能目录→catalog 每次重扫→断言排序后的 SkillManifest 精确并反映新增→失败判据：目录被缓存或字段错位。"""
    def make_skill(name: str, description: str) -> None:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({
            "name": name,
            "version": "1.0.0",
            "description": description,
            "triggers": [name],
        }), encoding="utf-8")

    make_skill("beta", "second")
    make_skill("alpha", "first")
    provider = FileSkillProvider(tmp_path)
    assert provider.catalog() == [
        SkillManifest(name="alpha", version="1.0.0", description="first",
                      triggers=("alpha",)),
        SkillManifest(name="beta", version="1.0.0", description="second",
                      triggers=("beta",)),
    ]
    make_skill("gamma", "third")
    assert [item.name for item in provider.catalog()] == ["alpha", "beta", "gamma"]


def test_prompt_capability_register_overrides_default_and_adds_new() -> None:
    """输入同名与新名模板→register 真注册→断言两次 render 消息列表精确→失败判据：同名未覆盖或新模板未生效。"""
    capability = PromptCapability()
    capability.register(PromptTemplate(
        name="default.system",
        system=Template("SYS:${role}"),
        user=Template("USER:${input}"),
    ))
    assert capability.render("default.system", role="ops", input="ping") == [
        {"role": "system", "content": "SYS:ops"},
        {"role": "user", "content": "USER:ping"},
    ]

    capability.register(PromptTemplate(
        name="new",
        system=Template("S:${input}"),
        user=Template("U:${input}"),
    ))
    assert capability.render("new", input="x") == [
        {"role": "system", "content": "S:x"},
        {"role": "user", "content": "U:x"},
    ]
