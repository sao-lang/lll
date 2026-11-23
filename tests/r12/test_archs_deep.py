"""R12 十架构 compile() 深度行为回归（真实 Runner + 确定性本地 call）。

本文件只新增测试，不改动源码/门禁/既有测试。每个用例都用 Runner 驱动
arch.compile() 产出的真实 generator，call 返回确定性 Result.success(...)，
断言真实的 status/output/reason 或 Effect 载荷，不使用 assert True/自证断言。
"""
from __future__ import annotations

import asyncio

from archs import (
    Blackboard, Graph, Handoff, IntentResult, MajorityVote, PlanExecute, ReAct,
    Reflection, Router, RouterSkill, StaticCheckReport, Supervisor, ToolUse,
)
from compose import ToolSpec
from runtime import Invoke, Result, Runner, Session, Spawn, Terminate


def _run(arch, *, call, sid: str = "arch-deep") -> tuple[str, object, str]:
    """用真实 Runner 驱动架构，返回 (status, output, reason)。"""
    res = asyncio.run(Runner(call=call).run(
        Session(session_id=sid, program_factory=lambda: arch.compile())))
    return res.status, res.output, res.reason


class _Named:
    """最小具名策略（planner/executor/replanner/reflector/child_spec）。"""
    def __init__(self, name: str, prompt: str = "") -> None:
        self.name = name
        self.prompt = prompt


class _TermArm:
    """最小 Arch：compile() 直接 Terminate(output=...)，用于子 Program 分支。"""
    name = "term-arm"
    version = "1.0"

    def __init__(self, out: object) -> None:
        self.out = out

    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(arch=self.name, version=self.version)

    def compile(self):
        def p():
            yield Terminate(output=self.out)
            return None
        return p()


class _DirectStrategy:
    """带 build() 的意图策略：直接把 IntentResult 作为子 Program 的 Terminate.output。"""
    def build(self, text: str):
        def p():
            yield Terminate(output=IntentResult(label="b", confidence=0.8))
            return None
        return p()


class _Skill:
    """最小 SkillSpec（name + 可选 downstream）。"""
    def __init__(self, name: str, downstream=None) -> None:
        self.name = name
        self.downstream = downstream


class _FirstJoin:
    name = "any"

    def join(self, results: list) -> object:
        return results[0] if results else None


class _Writer:
    def __init__(self, name: str, *writes: str) -> None:
        self.name = name
        self.writes = writes


def _node_program(label: str, seen: list[str]):
    """Graph 节点工厂：记录执行顺序并以带标签的 dict 结束。"""
    def p():
        seen.append(label)
        yield Invoke(port="llm", payload={"node": label})
        yield Terminate(output={"node": label})
        return None
    return p


# --------------------------------------------------------------------------- #
# ① ToolUse：并行 batch / 串行 seq / 空工具 / static_check
# --------------------------------------------------------------------------- #
def test_tooluse_parallel_expands_batch_and_returns_all_results() -> None:
    """并行：batch Invoke 展开为独立 tool Effect，回填 list 保序。"""
    seen: list[str] = []

    def call(effect, ctx):
        call_payload = effect.payload.get("call") or {}
        name = call_payload.get("name") if isinstance(call_payload, dict) else None
        seen.append(name)
        return Result.success({"tool": name, "n": len(seen)})

    arch = ToolUse([ToolSpec(name="a"), ToolSpec(name="b"), ToolSpec(name="c")])
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == [
        {"tool": "a", "n": 1}, {"tool": "b", "n": 2}, {"tool": "c", "n": 3},
    ]
    assert seen == ["a", "b", "c"]


def test_tooluse_serial_collects_each_result_in_order() -> None:
    """串行：逐个 Invoke，回填值按声明顺序收集。"""
    seen: list[str] = []

    def call(effect, ctx):
        seen.append(effect.payload.get("name"))
        return Result.success({"at": len(seen)})

    arch = ToolUse([ToolSpec(name="x"), ToolSpec(name="y")], parallel=False)
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == [{"at": 1}, {"at": 2}]
    assert seen == ["x", "y"]


def test_tooluse_empty_tools_terminates_with_tool_all_failed() -> None:
    """空工具目录：不调用任何 port，直接以 tool_all_failed 结束。"""
    called: list[object] = []
    arch = ToolUse([])
    status, output, reason = _run(
        arch, call=lambda e, c: called.append(e) or Result.success(None))
    assert status == "done"
    assert output == []
    assert reason == "tool_all_failed"
    assert called == []


def test_tooluse_static_check_reports_empty_and_unnamed_tools() -> None:
    """装配期：空目录报 E07，无名 ToolSpec 报 E09。"""
    assert "R12-E07" in {i.code for i in ToolUse([]).static_check().errors}
    assert "R12-E09" in {
        i.code for i in ToolUse([ToolSpec(name="")]).static_check().errors
    }


# --------------------------------------------------------------------------- #
# ② ReAct：model 注入 / 历史累积 / 轮次耗尽 / static_check
# --------------------------------------------------------------------------- #
def test_react_with_model_builds_growing_history() -> None:
    """带 llm 时注入 model；每轮把 assistant+tool 消息累积进 payload.messages。"""
    models: list[object] = []
    msg_counts: list[int] = []

    def call(effect, ctx):
        if effect.port == "llm":
            models.append(effect.payload.get("model"))
            msg_counts.append(len(effect.payload.get("messages", [])))
            if len(models) == 1:
                return Result.success({
                    "content": "calling", "tool_calls": [{"name": "t", "args": {}}],
                })
            return Result.success({"content": "finished", "tool_calls": []})
        return Result.success({"value": 42})

    arch = ReAct([ToolSpec(name="t")], llm="gpt-local", max_turns=5)
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == "finished"
    assert models == ["gpt-local", "gpt-local"]
    assert msg_counts == [0, 2]  # 第一轮空历史；第二轮含 1 assistant + 1 tool


def test_react_max_turns_exhausted_when_tool_calls_never_stop() -> None:
    """port 永不收敛：max_turns 作防御上限，reason=turns_exhausted。"""
    llm_runs = {"n": 0}

    def call(effect, ctx):
        if effect.port == "llm":
            llm_runs["n"] += 1
            return Result.success({
                "content": "again", "tool_calls": [{"name": "t"}],
            })
        return Result.success({"v": 1})

    arch = ReAct([ToolSpec(name="t")], llm="m", max_turns=3)
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert reason == "turns_exhausted"
    assert llm_runs["n"] == 3


def test_react_static_check_budget_and_port_errors() -> None:
    """装配期：空工具 E07 / 指定 llm 无错 / turns<=0 E17 / 超全局上限 E12。"""
    base = [ToolSpec(name="t")]
    assert "R12-E07" in {i.code for i in ReAct([]).static_check().errors}
    assert ReAct(base, llm="m").static_check().ok
    assert "R12-E17" in {
        i.code for i in ReAct(base, max_turns=0).static_check().errors
    }
    assert "R12-E12" in {
        i.code for i in ReAct(base, max_turns=9999).static_check().errors
    }


# --------------------------------------------------------------------------- #
# ③ PlanExecute：plan→execute / content 回退 / replan / replan 耗尽
# --------------------------------------------------------------------------- #
def test_planexecute_executes_each_step_and_returns_last() -> None:
    """计划产出 steps 列表；逐步按 step.port/payload 执行，output 为最后一步结果。"""
    ports: list[str] = []
    llm_runs = {"n": 0}

    def call(effect, ctx):
        ports.append(effect.port)
        if effect.port == "llm":
            llm_runs["n"] += 1
            if llm_runs["n"] == 1:  # 规划调用
                return Result.success({"steps": [
                    {"port": "tool", "payload": {"name": "s1"}},
                    {"port": "tool", "payload": {"name": "s2"}},
                ]})
            return Result.success({"ok": True, "from": "llm-step"})
        return Result.success({"ok": True, "from": effect.payload.get("name")})

    arch = PlanExecute(_Named("planner", "make-plan"), _Named("exec"))
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert ports == ["llm", "tool", "tool"]
    assert output == {"ok": True, "from": "s2"}


def test_planexecute_plan_content_fallback_creates_single_step() -> None:
    """规划只回 content 时，回退为单步 llm 执行。"""
    ports: list[str] = []

    def call(effect, ctx):
        ports.append(effect.port)
        return Result.success({"content": "plain instruction"})

    arch = PlanExecute(_Named("p"), _Named("e"))
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert ports == ["llm", "llm"]
    assert output == {"content": "plain instruction"}


def test_planexecute_replans_when_step_reports_failure() -> None:
    """步骤失败且配置了 replanner：额外发起一次 llm 重规划；output 仍是失败步结果。"""
    llm_runs = {"n": 0}

    def call(effect, ctx):
        if effect.port == "llm":
            llm_runs["n"] += 1
            if llm_runs["n"] == 1:  # plan
                return Result.success({"steps": [
                    {"port": "tool", "payload": {"name": "first"}},
                ]})
            return Result.success({"steps": [
                {"port": "tool", "payload": {"name": "retry"}},
            ]})  # replan
        return Result.success({"ok": False, "error": "boom"})

    arch = PlanExecute(_Named("p"), _Named("e"), replanner=_Named("rp", "replan"))
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert llm_runs["n"] == 2  # plan + replan
    assert output == {"ok": False, "error": "boom"}


def test_planexecute_replan_budget_exhaustion_reason() -> None:
    """max_replans=0 时失败步无法重规划，以 replan_exhausted 收尾。"""
    def call(effect, ctx):
        if effect.port == "llm":
            return Result.success({"steps": [
                {"port": "tool", "payload": {"name": "a"}},
            ]})
        return Result.success({"ok": False, "error": "fail"})

    arch = PlanExecute(_Named("p"), _Named("e"), replanner=_Named("rp"))
    arch.max_replans = 0  # compile() 通过 hasattr 读取的可选预算覆盖
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == {"ok": False, "error": "fail"}
    assert reason == "replan_exhausted"


def test_planexecute_static_check_flags_unfulfilled_roles() -> None:
    """装配期：planner/executor 未履约报 E15。"""
    codes = {i.code for i in PlanExecute(_Named(""), None).static_check().errors}
    assert "R12-E15" in codes


# --------------------------------------------------------------------------- #
# ④ Reflection：critique 失败→修订→通过 / 轮次耗尽 / static_check
# --------------------------------------------------------------------------- #
def test_reflection_revises_until_critique_passed() -> None:
    """draft→critique(fail)→revise→critique(pass)，输出通过时的最佳草稿。"""
    phases: list[str] = []

    def call(effect, ctx):
        phase = effect.payload["messages"][0]["content"]
        phases.append(phase)
        if phase == "draft":
            return Result.success({"content": "v1"})
        if phase == "critique":
            return Result.success({"passed": len(phases) > 3, "comments": "x"})
        if phase == "revise":
            return Result.success({"content": "v2"})
        return Result.success({"passed": True})

    arch = Reflection(_Named("critic", "critique"), max_rounds=3)
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == "v2"
    assert phases == ["draft", "critique", "revise", "critique"]


def test_reflection_exhausts_rounds_with_never_passing_critique() -> None:
    """critique 始终不通过：max_rounds 耗尽，reason=rounds_exhausted。"""
    revisions = {"n": 0}

    def call(effect, ctx):
        phase = effect.payload["messages"][0]["content"]
        if phase == "draft":
            return Result.success({"content": "v0"})
        if phase == "critique":
            return Result.success({"passed": False, "comments": "no"})
        revisions["n"] += 1
        return Result.success({"content": f"v{revisions['n']}"})

    arch = Reflection(_Named("critic", "critique"), max_rounds=2)
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert reason == "rounds_exhausted"
    assert output == "v2"
    assert revisions["n"] == 2


def test_reflection_static_check_flags_bad_budget_and_reflector() -> None:
    """装配期：rounds<=0 报 E17，reflector 未履约报 E15。"""
    codes = {i.code for i in Reflection(_Named(""), max_rounds=0).static_check().errors}
    assert "R12-E17" in codes
    assert "R12-E15" in codes


def test_reflection_static_check_accepts_valid_config() -> None:
    """装配期：合法 reflector + 正 rounds 无错，预算解析出 rounds。"""
    report = Reflection(_Named("critic", "critique"), max_rounds=2).static_check()
    assert report.ok
    assert report.budgets_resolved == {"rounds": 2}


def test_majority_vote_empty_results_is_none() -> None:
    """默认 join 策略：空结果集合并结果 None。"""
    assert MajorityVote().join([]) is None


# --------------------------------------------------------------------------- #
# ⑤ Router：label 命中 / 低置信 default / no_route / ProgramFn / static_check
# --------------------------------------------------------------------------- #
def test_router_no_route_when_unknown_label_and_no_default() -> None:
    """标签不在 arms 且无 default：以 no_route 结束，不执行任何 arm。"""
    arch = Router(
        lambda t: IntentResult(label="unknown", confidence=0.9),
        {"a": _TermArm("a")}, default=None,
    )
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output is None
    assert reason == "no_route"


def test_router_accepts_programfn_arm() -> None:
    """arm 直接是 ProgramFn（无 compile）时也能被驱动并透传其输出。"""
    def arm():
        yield Invoke(port="llm", payload={"tag": "fn-arm"})
        yield Terminate(output="fn-result")
        return None

    arch = Router(lambda t: IntentResult(label="fn", confidence=1.0), {"fn": arm})
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output == "fn-result"


def test_router_intentresult_from_build_skips_dict_normalization() -> None:
    """build() 子 Program 直接产出 IntentResult 对象时按对象分支读取 label/confidence。"""
    arch = Router(
        _DirectStrategy(),
        {"a": _TermArm("a-result"), "b": _TermArm("b-result")},
        default="a",
    )
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output == "b-result"


def test_router_noop_arm_returns_none() -> None:
    """空子 Program（yield from 后立即 StopIteration）被 _run_child 折为 None。"""
    def noop():
        if False:  # pragma: no cover - 仅用于把它标记为 generator
            yield Invoke(port="llm", payload={})
        return None

    arch = Router(lambda t: IntentResult(label="noop", confidence=1.0), {"noop": noop})
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output is None


def test_router_static_check_flags_empty_arms_and_bad_default() -> None:
    """装配期：arms 为空报 E07，default 不在 arms 报 E08。"""
    assert "R12-E07" in {
        i.code for i in Router(lambda t: None, {}).static_check().errors
    }
    assert "R12-E08" in {
        i.code for i in Router(
            lambda t: None, {"a": _TermArm("a")}, default="z",
        ).static_check().errors
    }


# --------------------------------------------------------------------------- #
# ⑥ RouterSkill：skill body 加载 + downstream / callable / ProgramFn / default
# --------------------------------------------------------------------------- #
def test_routerskill_loads_skill_body_then_runs_arch_downstream() -> None:
    """SkillSpec 路由：先发 skill.body Invoke，再驱动下游 Arch 子 Program。"""
    effects: list[tuple[str, dict]] = []

    def call(effect, ctx):
        effects.append((effect.port, dict(effect.payload)))
        return Result.success({"loaded": True})

    skill = _Skill("skill-a", downstream=_TermArm("down-out"))
    arch = RouterSkill(
        lambda t: IntentResult(label="sk", confidence=0.9),
        {"sk": skill}, default="sk",
    )
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == "down-out"
    skill_invokes = [p for port, p in effects if port == "skill"]
    assert skill_invokes == [{"name": "skill-a", "part": "body"}]


def test_routerskill_skill_downstream_callable() -> None:
    """downstream 是无 compile 的可调用对象：作为 ProgramFn 驱动。"""
    def downstream():
        yield Invoke(port="tool", payload={"k": 1})
        yield Terminate(output="callable-out")
        return None

    skill = _Skill("skill-b", downstream=downstream)
    arch = RouterSkill(
        lambda t: IntentResult(label="sk", confidence=0.9),
        {"sk": skill}, default="sk",
    )
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output == "callable-out"


def test_routerskill_programfn_route() -> None:
    """路由值直接是 ProgramFn（无 name、无 compile）：走 callable 分支。"""
    def route():
        yield Invoke(port="llm", payload={"k": 2})
        yield Terminate(output="pf-out")
        return None

    arch = RouterSkill(
        lambda t: IntentResult(label="pf", confidence=0.9),
        {"pf": route}, default="pf",
    )
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output == "pf-out"


def test_routerskill_arch_route_without_downstream_falls_back_to_label() -> None:
    """Arch 路由值被当作 SkillSpec 加载 body，无 downstream 时回退输出意图标签。"""
    arch = RouterSkill(
        lambda t: IntentResult(label="lbl", confidence=0.9),
        {"lbl": _TermArm("ignored")}, default="lbl",
    )
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output == "lbl"


def test_routerskill_default_and_no_route() -> None:
    """标签缺失走 default；default 也不存在则 no_route。"""
    def route():
        yield Terminate(output="default-out")
        return None

    by_label = RouterSkill(
        lambda t: IntentResult(label="missing", confidence=0.9),
        {"d": route}, default="d",
    )
    status, output, reason = _run(by_label, call=lambda e, c: Result.success(None))
    assert status == "done" and output == "default-out"

    no_route = RouterSkill(
        lambda t: IntentResult(label="missing", confidence=0.9),
        {"d": route}, default="absent",
    )
    status, output, reason = _run(no_route, call=lambda e, c: Result.success(None))
    assert status == "done" and output is None and reason == "no_route"


def test_routerskill_intentresult_from_build() -> None:
    """build() 直接产出 IntentResult 时走对象分支（RouterSkill 的 385 行）。"""
    def route():
        yield Terminate(output="b-route")
        return None

    arch = RouterSkill(_DirectStrategy(), {"b": route}, default="a")
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output == "b-route"


# --------------------------------------------------------------------------- #
# ⑦ Supervisor：拆任务→Spawn 子查询→join / child factory / 空任务
# --------------------------------------------------------------------------- #
def test_supervisor_splits_spawns_children_and_joins() -> None:
    """LLM 拆出 tasks；每个 task 派生子 Agent 执行 llm；默认多数投票合并。"""
    seen: list[object] = []

    class _BuildSpec:
        name = "spec"

        def build(self, task):
            def child():
                data = yield Invoke(port="llm", payload={"task": task})
                yield Terminate(output=f"{task}:{data['v']}")
                return None
            return child

    def call(effect, ctx):
        if effect.port == "llm":
            content = effect.payload.get("messages", [{}])[0].get("content")
            if content == "split":
                return Result.success({"tasks": ["t1", "t2", "t3"]})
        seen.append(effect.payload.get("task"))
        return Result.success({"v": effect.payload.get("task")})

    arch = Supervisor(_Named("p", "split"), _BuildSpec())
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert seen == ["t1", "t2", "t3"]  # 三个 task 全部派生为子 Agent
    assert output == "t1:t1"  # 三个不同值平票 → MajorityVote 取首个


def test_supervisor_child_without_factory_returns_no_output() -> None:
    """child_spec 无构造工厂时，子 Agent 只发一次 llm 且不产终端输出。"""
    seen: list[object] = []

    def call(effect, ctx):
        if effect.port == "llm":
            content = effect.payload["messages"][0]["content"]
            if content == "split":
                return Result.success({"tasks": ["x", "y"]})
        seen.append(effect.payload["messages"][0]["content"])
        return Result.success({"ok": True})

    arch = Supervisor(_Named("p", "split"), _Named("child-spec"))
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert seen == ["x", "y"]
    assert output is None  # 子 Program 无 Terminate，Runner 合成空 output


def test_supervisor_child_spec_with_program_factory() -> None:
    """child_spec.program_factory(task) 存在时走子 Program 工厂分支 + 自定义 join。"""
    class _Spec:
        name = "spec"

        def program_factory(self, task):
            def child():
                yield Invoke(port="tool", payload={"task": task})
                yield Terminate(output={"done": task})
                return None
            return child

    def call(effect, ctx):
        if effect.port == "llm":
            return Result.success({"tasks": ["a", "b"]})
        return Result.success({"echo": effect.payload.get("task")})

    arch = Supervisor(_Named("p"), _Spec(), max_children=2, join=_FirstJoin())
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == {"done": "a"}


def test_supervisor_no_tasks_terminates_with_join_timeout() -> None:
    """空 tasks 不派生子 Agent，以 join_timeout 结束。"""
    arch = Supervisor(_Named("p"), _Named("c"))
    status, output, reason = _run(
        arch, call=lambda e, c: Result.success({"tasks": []}))
    assert status == "done"
    assert output == []
    assert reason == "join_timeout"


def test_supervisor_static_check_flags_unfulfilled_planner() -> None:
    """装配期：planner 未履约报 E15。"""
    codes = {
        i.code for i in Supervisor(_Named(""), _Named("c")).static_check().errors
    }
    assert "R12-E15" in codes


# --------------------------------------------------------------------------- #
# ⑧ Handoff：声明顺序接力 / 显式交接目标 / Arch/ProgramFn 角色 / 跳数耗尽
# --------------------------------------------------------------------------- #
def test_handoff_relays_roles_in_declaration_order() -> None:
    """非 Program 角色按声明顺序逐个交接，output 为末位角色结果。"""
    seen: list[str] = []

    def call(effect, ctx):
        name = effect.payload.get("name")
        seen.append(name)
        return Result.success({"role": name})

    arch = Handoff({"a": object(), "b": object(), "c": object()})
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert seen == ["a", "b", "c"]
    assert output == {"role": "c"}


def test_handoff_follows_explicit_handoff_target() -> None:
    """结果携带 x-arch.handoff.role 时跳到指定角色（跳过中间角色）。"""
    seen: list[str] = []

    def call(effect, ctx):
        name = effect.payload.get("name")
        seen.append(name)
        if name == "a":
            return Result.success({"x-arch.handoff.role": "c"})
        return Result.success({"role": name})

    arch = Handoff({"a": object(), "b": object(), "c": object()})
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert seen == ["a", "c"]
    assert output == {"role": "c"}


def test_handoff_runs_arch_and_programfn_roles() -> None:
    """Arch 角色经子 Program 驱动；ProgramFn 角色直接驱动；output 为最后角色。"""
    def role_program():
        yield Invoke(port="llm", payload={"tag": "fn-role"})
        yield Terminate(output="fn-role-out")
        return None

    def call(effect, ctx):
        return Result.success({"tag": effect.payload.get("tag")})

    arch = Handoff({
        "arch": _TermArm("arch-out"),
        "fn": role_program,
        "plain": object(),
    })
    status, output, reason = _run(arch, call=call)
    assert status == "done"
    assert output == {"tag": None}  # plain 角色的 Invoke 无 tag


def test_handoff_hops_exhausted_and_empty_roles() -> None:
    """角色数超过 max_hops 报 hops_exhausted；空 roles 报 no_route。"""
    arch = Handoff({"a": object(), "b": object(), "c": object()}, max_hops=2)
    status, output, reason = _run(arch, call=lambda e, c: Result.success({"v": 1}))
    assert status == "done" and reason == "hops_exhausted"

    empty = Handoff({})
    status, output, reason = _run(empty, call=lambda e, c: Result.success(None))
    assert status == "done" and output is None and reason == "no_route"


def test_handoff_static_check_flags_bad_budget() -> None:
    """装配期：roles 为空报 E07，max_hops<=0 报 E17。"""
    codes = {
        i.code for i in Handoff({}, max_hops=0).static_check().errors
    }
    assert "R12-E07" in codes
    assert "R12-E17" in codes


# --------------------------------------------------------------------------- #
# ⑨ Blackboard：多代理并发竞速 / 首写胜出 / 轮次 / 包装非 writes 输出
# --------------------------------------------------------------------------- #
def test_blackboard_merges_agent_writes_first_writer_wins() -> None:
    """多 agent 并行写共享键；冲突键首个胜出；round 计数与 board 快照可见。"""
    def agent_a():
        yield Invoke(port="llm", payload={"who": "a"})
        yield Terminate(output={"writes": {"k": "from-a"}})
        return None

    def agent_b():
        yield Invoke(port="llm", payload={"who": "b"})
        yield Terminate(output={"writes": {"k": "from-b", "other": 1}})
        return None

    arch = Blackboard([agent_a, agent_b], rounds=2)
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output["k"] == "from-a"  # 冲突键首个 agent 胜出
    assert output["other"] == 1
    assert output["x-arch.bb.round"] == 2
    assert output["x-arch.bb.board"]["k"] == "from-a"


def test_blackboard_wraps_plain_arch_output_as_result_write() -> None:
    """Arch 子 Agent 输出非 writes 信封时，包装为 result 键写入黑板。"""
    arch = Blackboard([_TermArm("a-out"), _TermArm("b-out")], rounds=1)
    status, output, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert output["result"] == "a-out"
    assert output["x-arch.bb.round"] == 1


def test_blackboard_static_check_empty_agents_and_bad_rounds() -> None:
    """装配期：agents 为空报 E07，rounds<=0 报 E17，独立写键不报 E16。"""
    codes = {
        i.code for i in Blackboard([], rounds=0).static_check().errors
    }
    assert "R12-E07" in codes
    assert "R12-E17" in codes
    assert Blackboard([_Writer("a", "k1"), _Writer("b", "k2")]).static_check().ok


# --------------------------------------------------------------------------- #
# ⑩ Graph：entry→边→cond→exit / 无出边 / 运行期环 / cond 异常 / 静态检查
# --------------------------------------------------------------------------- #
def test_graph_traverses_unconditional_edges_to_exit() -> None:
    """无条件边按声明顺序串接节点，output 为 exit 节点结果。"""
    seen: list[str] = []
    graph = (Graph()
             .node("start", _node_program("start", seen))
             .node("mid", _node_program("mid", seen))
             .node("done", _node_program("done", seen))
             .edge("start", "mid")
             .edge("mid", "done")
             .exit("done"))
    status, output, reason = _run(graph, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert seen == ["start", "mid", "done"]
    assert output == {"node": "done"}


def test_graph_stops_when_node_has_no_outgoing_edge() -> None:
    """非 exit 节点无出边：提前停止，output 为该节点结果。"""
    seen: list[str] = []
    graph = (Graph()
             .node("a", _node_program("a", seen))
             .node("unused", _node_program("unused", seen))
             .exit("unused"))
    status, output, reason = _run(graph, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert seen == ["a"]
    assert output == {"node": "a"}


def test_graph_runtime_cycle_terminates_with_cycle_reason() -> None:
    """编译期不检查环；运行期重访节点时以 cycle 结束。"""
    seen: list[str] = []
    graph = (Graph()
             .node("a", _node_program("a", seen))
             .node("b", _node_program("b", seen))
             .node("exit", _node_program("exit", seen))
             .edge("a", "b")
             .edge("b", "a")
             .exit("exit"))
    status, output, reason = _run(graph, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert seen == ["a", "b"]
    assert output is None
    assert reason == "cycle"


def test_graph_cond_exception_and_none_fall_through_to_unconditional() -> None:
    """cond 抛异常或返回 None 时均视为未命中，走无条件边。"""
    seen: list[str] = []

    def boom(state):
        raise RuntimeError("decided at runtime")

    graph = (Graph()
             .node("s", _node_program("s", seen))
             .node("x", _node_program("x", seen))
             .node("y", _node_program("y", seen))
             .node("fallback", _node_program("fallback", seen))
             .edge("s", "x", cond=boom)
             .edge("s", "y", cond=lambda state: None)
             .edge("s", "fallback")
             .exit("fallback"))
    status, output, reason = _run(graph, call=lambda e, c: Result.success(None))
    assert status == "done"
    assert seen == ["s", "fallback"]
    assert output == {"node": "fallback"}


def test_graph_invalid_graph_without_nodes_or_exit() -> None:
    """无 entry 或无 exit：以 graph_invalid 结束，且 static_check 报 E04。"""
    def call(effect, ctx):
        raise AssertionError("invalid graph must not call any port")

    status, output, reason = _run(Graph(), call=call)
    assert status == "done"
    assert output is None
    assert reason == "graph_invalid"
    assert "R12-E04" in {i.code for i in Graph().static_check().errors}


def test_graph_static_check_dangling_edge_and_runtime_only_cond() -> None:
    """静态检查：悬空边报 E01；运行期 cond 异常不在装配期误报。"""
    def p():
        yield Terminate(output=None)
        return None

    def late_cond(state):
        raise RuntimeError("only known at runtime")

    dangling = Graph().node("a", p).edge("a", "ghost").exit("a")
    assert "R12-E01" in {i.code for i in dangling.static_check().errors}

    with_cond = (Graph().node("a", p).node("b", p)
                 .edge("a", "b", cond=late_cond).exit("b"))
    assert with_cond.static_check().ok
