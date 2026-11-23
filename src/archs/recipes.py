"""10 内置架构 = A 类算子的预设配方（R12 §6，规格 P6）。

每个架构实现 Arch 协议（name/version/static_check/compile），compile() 产出真实
语义的 Program：按 R12 §6.1.2 的骨架实现 phase 机、反思循环、条件路由、分支跳转
等架构行为；R1 只认 Program，架构知识全在装配期。构造签名对齐 R12 §4「对外 API 不变」。

运行时建模（已用 Runner 实测确认）：program `yield Effect` 后 `send` 回填的是
port 的 `Result.data`——单个 Invoke 收 dict，batch Invoke 收 list[dict]；AgentState
只由 R1 fold 内部生成、program 拿不到。故架构内所有决策（tool_calls/label/passed/
next 节点等）都从回注值读，多步进度用 Invoke.payload 的 `x-arch.*` 扩展键随 Effect
传递（D-2 约定），不依赖 fold 回注完整 State。

预算（max_turns/max_rounds/max_hops）是 Budgets 维度（R1 强制扣减）：generator 内
的 while 循环用构造参数作防御性上限（防止 port 永不收敛时死循环），真实约束仍由
R1 Budgets 兜底；static_check 把预算记入 budgets_resolved 供装配期核对。
"""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from compose import DangerLevel, ToolSpec
from compose.program import ProgramFn, branch_value, loop_until, map_effect, par, seq
from harness.cognition import intent_as_program
from runtime import AgentState, DynamicValue, Effect, Invoke, Program, Spawn, Terminate

from .base import (
    Arch, ChildSpec, Executor, IntentResult, IntentStrategy, JoinStrategy,
    MajorityVote, Planner, RePlanner, ReflectStrategy, SkillSpec,
    StaticCheckIssue, StaticCheckReport,
)

__all__ = [
    "Blackboard",
    "Graph",
    "Handoff",
    "PlanExecute",
    "ReAct",
    "Reflection",
    "Router",
    "RouterSkill",
    "Supervisor",
    "ToolUse",
]


def _report(name: str, errors: tuple[StaticCheckIssue, ...], ports: tuple[str, ...],
            budgets: dict[str, int | float] | None = None) -> StaticCheckReport:
    return StaticCheckReport(errors=errors, arch=name, version="12.0.0",
                             ports_required=ports, budgets_resolved=budgets or {})


# --------------------------------------------------------------------------- #
# 内部 helper：回注值归一 + Effect 构造 + x-arch.* 进度传递（R12 §6.1.1）
# --------------------------------------------------------------------------- #
class _X:
    """架构私有 scratch 键前缀（集中定义禁用魔法串，R12 §6.1.1）。"""
    PREFIX: Final[str] = "x-arch."

    @staticmethod
    def of(arch: str, key: str) -> str:
        """返回 `x-arch.<arch>.<key>` 键名。"""
        return f"{_X.PREFIX}{arch}.{key}"


def _dict(value: DynamicValue) -> dict[str, DynamicValue]:
    """把回注值归一为可读写 dict（非映射返回空 dict）。

    Runner 实测：单 Invoke 回填 Result.data（通常 dict），batch 回填 list[dict]。
    """
    return dict(value) if isinstance(value, Mapping) else {}


def _get(data: Mapping[str, DynamicValue], key: str, default: DynamicValue = None) -> DynamicValue:
    """从回注映射读字段，None 视为缺失回退 default。"""
    value = data.get(key)
    return default if value is None else value


def _invoke(port: str, payload: dict[str, DynamicValue] | None = None,
            **extra: DynamicValue) -> Invoke:
    """构造 Invoke（extra 覆盖 payload 同名键）。"""
    data: dict[str, DynamicValue] = dict(payload or {})
    data.update(extra)
    return Invoke(port=port, payload=data)  # type: ignore[arg-type]


def _batch(port: str, calls: Sequence[Mapping[str, DynamicValue]],
           **extra: DynamicValue) -> Invoke:
    """构造批量 Invoke（payload.calls 由解释器展开为独立 Effect）。"""
    data: dict[str, DynamicValue] = {"calls": [dict(c) for c in calls], **extra}
    return Invoke(port=port, batch=True, payload=data)  # type: ignore[arg-type]


def _tool_calls(value: DynamicValue) -> list[dict[str, DynamicValue]]:
    """从 LLM 回注值提取 tool_calls 列表（每项归一为 dict）。"""
    calls = _get(_dict(value), "tool_calls", []) or []
    return [c for c in (_dict(item) for item in calls) if c]


def _end(value: DynamicValue = None, reason: str = "") -> Terminate:
    """构造 Terminate Effect。"""
    return Terminate(output=value, reason=reason)


def _run_child(child: Program) -> Generator[Effect, DynamicValue, DynamicValue]:
    """驱动子 Program：转发其 Effect，拦截 Terminate 提取 output（不提前结束整个 run）。

    子 Program 内的 Terminate 代表"该子流程结束"，不能直接 yield 给 R1（会终止全部）；
    本驱动把它转成返回值，与 compose._run_child 语义一致但架构层自包含。

    参数：
        child: 子 Program（generator 对象）。
    返回：
        generator：产出子 Effect，最终返回子 Terminate.output。
    """
    try:
        effect: Effect = next(child)
        while not isinstance(effect, Terminate):
            sent: DynamicValue = yield effect
            effect = child.send(sent)
        return effect.output
    except StopIteration:
        return None


@dataclass(frozen=True, slots=True)
class _Edge:
    """Graph 的有向边：src→dst，可带 cond(state)->目标 id 的条件分支。"""
    src: str
    dst: str
    cond: Callable[[AgentState], str | None] | None = None


class ToolUse:
    """① 一次性并行工具调用（parallel=True 用 par，否则 seq）。"""
    name = "ToolUse"; version = "12.0.0"

    def __init__(self, tools: list[ToolSpec], *, parallel: bool = True) -> None:
        self.tools = tools; self.parallel = parallel

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.tools:
            errs.append(StaticCheckIssue("R12-E07", "tools 为空", "tools"))
        for t in self.tools:
            # L1 披露只有 name+description，parameters_schema 缺省空 dict 合法（L2/L3 才披露）。
            if not t.name:
                errs.append(StaticCheckIssue("R12-E09", "ToolSpec 缺 name", t.name))
            # E10：高危工具未配沙盒（ToolUse 构造不含 SandboxPort → 高危声明即报）。
            declared = getattr(t, "danger", None)
            # 未声明 danger 属 E09 域；这里只处理显式高危且未配沙盒。
            if declared in (DangerLevel.HIGH, DangerLevel.CRITICAL):
                errs.append(StaticCheckIssue("R12-E10", f"{t.name} 高危未配沙盒", t.name))
        return _report(self.name, tuple(errs), ("tool",))

    def compile(self) -> Program:
        """ToolUse = par(并行工具) / reduce(seq, 串行工具)，由组合算子承担控制流。"""
        from functools import reduce

        calls = [{"name": t.name, "call": {"call_id": t.name, "name": t.name, "args": {}}}
                 for t in self.tools]
        if not calls:
            def empty() -> Program:
                yield _end([], "tool_all_failed")
                return None
            return empty()

        if self.parallel:
            def one(call: DynamicValue) -> ProgramFn:
                def child() -> Program:
                    def gen() -> Generator[Effect, DynamicValue, None]:
                        data = yield _invoke("tool", {"call": call})
                        yield Terminate(output=data)
                    return gen()
                return child
            # par 真实承担并行：子 Invoke 汇成列表一次 yield → R1 列表并行 → 按槽回注 → join 聚合。
            return par([one(c) for c in calls], join="all")()

        results_list: list[DynamicValue] = []

        def serial(call: DynamicValue) -> ProgramFn:
            def child() -> Program:
                def gen() -> Generator[Effect, DynamicValue, None]:
                    data = yield _invoke("tool", call)
                    results_list.append(data)
                    yield Terminate(output=list(results_list))
                return gen()
            return child
        # seq 真实承担串行：reduce 成链，上一个 child 的 Terminate 被拦截后才启动下一个。
        return reduce(seq, [serial(c) for c in calls])()


class ReAct:
    """② 推理-行动自循环（loop 承载；轮次交 Budgets）。"""
    name = "ReAct"; version = "12.0.0"

    def __init__(self, tools: list[ToolSpec], *, llm: str | None = None, max_turns: int = 30) -> None:
        self.tools = tools; self.llm = llm; self.max_turns = max_turns

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.tools:
            errs.append(StaticCheckIssue("R12-E07", "tools 为空", "tools"))
        if self.llm is None:
            errs.append(StaticCheckIssue("R12-E11", "llm port 未指定", "llm"))
        if self.max_turns <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_turns <= 0", "max_turns"))
        from runtime import Budgets
        if self.max_turns > Budgets().max_turns:
            errs.append(StaticCheckIssue("R12-E12", f"max_turns {self.max_turns} 超全局上限", "max_turns"))
        return _report(self.name, tuple(errs), ("llm", "tool"), {"turns": self.max_turns})

    def compile(self) -> Program:
        """ReAct = loop_until(llm→tools 步) + map_effect(消息累积)，由组合算子封装。

        决策基于 LLM 回注的 tool_calls（非 AgentState），故用 loop_until（回注值驱动，
        不读 state port，红队 B5）；每轮把 assistant/tool 消息累积进历史，交下一轮 LLM。
        """
        # 语义门面注入的用户输入（红队 B2）：作为首轮 user 消息，不靠用户在 call 里补。
        input_text = getattr(self, "input_text", "")
        messages: list[DynamicValue] = (
            [{"role": "user", "content": input_text}] if input_text else []
        )
        tools = [t.model_dump() if hasattr(t, "model_dump") else t for t in self.tools]

        def _step(_feedback: DynamicValue) -> ProgramFn:
            """一轮：LLM → （若有 tool_calls）工具 → 累积消息，产出本轮结论或工具结果。"""
            def one_round() -> Program:
                def program() -> Program:
                    payload: dict[str, DynamicValue] = {
                        "messages": [dict(m) for m in messages], "tools": tools,
                    }
                    if self.llm is not None:
                        payload["model"] = self.llm
                    llm_out = yield _invoke("llm", payload)
                    assistant = _dict(llm_out)
                    calls = _tool_calls(llm_out)
                    messages.append({"role": "assistant",
                                     "content": _get(assistant, "content", ""),
                                     "tool_calls": calls})
                    if not calls:
                        yield _end(_get(assistant, "content"))
                        return
                    tool_results = yield _batch("tool", calls)
                    results = tool_results if isinstance(tool_results, list) else [tool_results]
                    for call, data in zip(calls, results):
                        messages.append({"role": "tool", "name": _get(call, "name", ""),
                                         "content": data})
                    yield _end({"tool_results": results, "calls": calls})
                return program()
            return one_round

        def _done(out: DynamicValue) -> bool:
            """无 tool_calls 即收敛：结论轮返回的 dict 不带 calls 键。"""
            return not (isinstance(out, dict) and out.get("calls"))

        # seq 真实复用：首轮 step 由 loop_until 驱动，seq 保证终态 Terminate 语义。
        return loop_until(_step, done=_done, max_rounds=self.max_turns)()


class PlanExecute:
    """③ 规划-执行状态机（seq(plan, execute, [replan])）。"""
    name = "PlanExecute"; version = "12.0.0"

    def __init__(self, planner: Planner, executor: Executor, *,
                 replanner: RePlanner | None = None) -> None:
        self.planner = planner; self.executor = executor; self.replanner = replanner

    def static_check(self) -> StaticCheckReport:
        errs = []
        for role, obj in (("planner", self.planner), ("executor", self.executor)):
            if obj is None or not getattr(obj, "name", None):
                errs.append(StaticCheckIssue("R12-E15", f"{role} 未履约", role))
        return _report(self.name, tuple(errs), ("llm",))

    def compile(self) -> Program:
        """PlanExecute = plan → loop_until(步执行 + replan 预算)，由算子承担步循环。"""
        def program() -> Program:
            steps0 = self._parse_steps((yield _invoke("llm", {"messages": [
                {"role": "system", "content": getattr(self.planner, "prompt", "plan")}]})))
            budget0 = self.max_replans if hasattr(self, "max_replans") else len(steps0) + 1

            def _step(ctrl: DynamicValue) -> ProgramFn:
                def one_round() -> Program:
                    def gen() -> Generator[Effect, DynamicValue, None]:
                        if isinstance(ctrl, dict):
                            idx = int(ctrl.get("idx", 0)); last = ctrl.get("last")
                            b = int(ctrl.get("budget", budget0)); cur = ctrl.get("steps", steps0)
                        else:
                            idx, last, b, cur = 0, None, budget0, steps0
                        if idx >= len(cur):
                            yield Terminate(output={"idx": idx, "last": last,
                                                    "budget": b, "steps": cur})
                            return
                        step_map = _dict(cur[idx])
                        out = yield _invoke(str(_get(step_map, "port", "tool")),
                                            _dict(_get(step_map, "payload", {})))
                        failed = bool(_get(_dict(out), "ok", True) is False
                                      or _get(_dict(out), "error"))
                        if failed and self.replanner is not None and b > 0:
                            yield _invoke("llm", {"messages": [
                                {"role": "system",
                                 "content": getattr(self.replanner, "prompt", "replan")},
                                {"role": "user",
                                 "content": {"steps": cur, "failed": cur[idx], "last": out}},
                            ]})
                            b -= 1  # replan 结果不入 cur：保持旧列表迭代（与旧 for 等价）
                        yield Terminate(output={"idx": idx + 1, "last": out,
                                                "budget": b, "steps": cur})
                    return gen()
                return one_round

            def _done(out: DynamicValue) -> bool:
                return isinstance(out, dict) and int(out.get("idx", 0)) >= len(steps0)

            out = yield from _run_child(
                loop_until(_step, done=_done, max_rounds=max(1, len(steps0) + 1))())
            ctrl = out if isinstance(out, dict) else {}
            yield _end(ctrl.get("last"),
                       "replan_exhausted" if int(ctrl.get("budget", 1)) <= 0 else "")
            return None
        return program()

    @staticmethod
    def _parse_steps(value: DynamicValue) -> list[DynamicValue]:
        """从 LLM 回注解析步骤列表（约定 steps 键；退而取 content 文本为单步）。"""
        data = _dict(value)
        steps = _get(data, "steps")
        if isinstance(steps, list):
            return steps
        content = _get(data, "content")
        return [{"port": "llm", "payload": {"messages": [{"role": "user", "content": content}]}}] if content else []


class Reflection:
    """④ 起草-反思-修订（seq(draft, reflect, revise)；轮次交 Budgets）。"""
    name = "Reflection"; version = "12.0.0"

    def __init__(self, reflector: ReflectStrategy, *, max_rounds: int = 3) -> None:
        self.reflector = reflector; self.max_rounds = max_rounds

    def static_check(self) -> StaticCheckReport:
        errs = []
        if self.max_rounds <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_rounds <= 0", "max_rounds"))
        if not getattr(self.reflector, "name", None):
            errs.append(StaticCheckIssue("R12-E15", "reflector 未履约", "reflector"))
        return _report(self.name, tuple(errs), ("llm",), {"rounds": self.max_rounds})

    def compile(self) -> Program:
        """Reflection = draft → loop_until(critique→revise)，算子承担反思循环。"""
        def program() -> Program:
            first = _get(_dict((yield _invoke("llm", {"messages": [
                {"role": "system", "content": "draft"}]}))), "content", "") or ""

            def _step(best: DynamicValue) -> ProgramFn:
                # feedback 是上一轮产出 dict，取其中的 best 草稿文本（避免 dict 嵌套）。
                draft = str(_get(_dict(best), "best", best)) if isinstance(best, dict) else str(best)
                def one_round() -> Program:
                    def gen() -> Generator[Effect, DynamicValue, None]:
                        critique = _dict((yield _invoke("llm", {"messages": [
                            {"role": "system",
                             "content": getattr(self.reflector, "prompt", "critique")},
                            {"role": "user", "content": draft},
                        ]})))
                        if bool(_get(critique, "passed", False)):
                            yield Terminate(output={"best": draft, "passed": True})
                            return
                        revise_out = yield _invoke("llm", {"messages": [
                            {"role": "system", "content": "revise"},
                            {"role": "user", "content": {"draft": best, "critique": critique}},
                        ]})
                        yield Terminate(output={
                            "best": _get(_dict(revise_out), "content", draft) or draft,
                            "passed": False,
                        })
                    return gen()
                return one_round

            def _done(out: DynamicValue) -> bool:
                return isinstance(out, dict) and bool(out.get("passed", False))

            result = yield from _run_child(
                loop_until(_step, done=_done, max_rounds=self.max_rounds)())
            ctrl = result if isinstance(result, dict) else {}
            yield _end(ctrl.get("best", first),
                       "" if bool(ctrl.get("passed", False)) else "rounds_exhausted")
            return None
        return program()


class Router:
    """⑤ 意图路由（branch(fn, arms, default)）。"""
    name = "Router"; version = "12.0.0"

    def __init__(self, classifier: IntentStrategy | Callable[..., IntentResult] | None,
                 arms: Mapping[str, Arch | SkillSpec], *,
                 default: str | None = None, min_confidence: float = 0.0) -> None:
        self.classifier = classifier; self.arms = arms
        self.default = default; self.min_confidence = min_confidence

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.arms:
            errs.append(StaticCheckIssue("R12-E07", "arms 为空", "arms"))
        if self.default is not None and self.default not in self.arms:
            errs.append(StaticCheckIssue("R12-E08", "default 不在 arms", "default"))
        return _report(self.name, tuple(errs), ("llm",))

    def compile(self) -> Program:
        """Router = branch_value(意图源, 决策, arms)，由值驱动分支算子承担路由。"""
        def source() -> Program:
            def p() -> Program:
                raw = yield from _run_child(intent_as_program(self.classifier, text="")())
                yield _end(raw)
                return None
            return p()

        def key(raw: DynamicValue) -> str:
            intent = raw if isinstance(raw, IntentResult) else IntentResult(
                label=str(_get(_dict(raw), "label", "")),
                confidence=float(_get(_dict(raw), "confidence", 0.0)))
            return (intent.label if intent.confidence >= self.min_confidence
                    and intent.label in self.arms else str(self.default or ""))

        arms: dict[str, ProgramFn] = {
            k: cast(ProgramFn, v.compile if hasattr(v, "compile") else v)
            for k, v in self.arms.items()
        }
        return branch_value(source, key, arms, no_route_reason="no_route")()


class RouterSkill(Router):
    """⑥ 技能路由（Router 变体，routes 可含 SkillSpec）。"""
    name = "RouterSkill"

    def __init__(self, classifier: IntentStrategy | Callable[..., IntentResult] | None,
                 routes: Mapping[str, Arch | SkillSpec], default: str) -> None:
        # 签名按 R12 §4 保留 routes 名；值类型与基类 arms 一致，
        # 只读映射可直接向上传递。
        super().__init__(classifier, routes, default=default)

    def compile(self) -> Program:
        """RouterSkill = branch_value(路由) + seq(skill.body, downstream)，算子承担路由与顺序。"""
        def source() -> Program:
            def p() -> Program:
                raw = yield from _run_child(intent_as_program(self.classifier, text="")())
                yield _end(raw)
                return None
            return p()

        def key(raw: DynamicValue) -> str:
            intent = raw if isinstance(raw, IntentResult) else IntentResult(
                label=str(_get(_dict(raw), "label", "")),
                confidence=float(_get(_dict(raw), "confidence", 0.0)))
            if intent.confidence < self.min_confidence or intent.label not in self.arms:
                return str(self.default or "")
            return intent.label

        def arm_program(route: DynamicValue, intent_label: str) -> ProgramFn:
            def p() -> Program:
                def program() -> Program:
                    if isinstance(route, SkillSpec):
                        def load_body() -> Program:
                            def g() -> Program:
                                yield _invoke("skill", {"name": route.name, "part": "body"})
                                yield Terminate(output=None)
                                return None
                            return g()
                        downstream = getattr(route, "downstream", None)
                        if downstream is not None:
                            def run_down() -> Program:
                                def g() -> Program:
                                    factory = (downstream.compile
                                               if isinstance(downstream, Arch) else downstream)
                                    out = yield from _run_child(factory())
                                    yield Terminate(output=out)
                                    return None
                                return g()
                            # seq 真实承担「加载技能正文 → 执行下游」顺序
                            out = yield from _run_child(seq(load_body, run_down)())
                            yield _end(out)
                            return None
                        yield _end(_get(_dict(route), "name", intent_label))
                        return None
                    if isinstance(route, Arch):
                        out = yield from _run_child(route.compile())
                    else:
                        out = yield from _run_child(cast(ProgramFn, route)())
                    yield _end(out)
                    return None
                return program()
            return p

        arms = {k: arm_program(v, k) for k, v in self.arms.items()}
        return branch_value(source, key, arms, no_route_reason="no_route")()


class Supervisor:
    """⑦ 监督者-子代理（par(planner, children)；子数交 Budgets）。"""
    name = "Supervisor"; version = "12.0.0"

    def __init__(self, planner: Planner, child_spec: ChildSpec, *, max_children: int = 8,
                 join: JoinStrategy | None = None) -> None:
        self.planner = planner; self.child_spec = child_spec
        self.max_children = max_children; self.join = join or MajorityVote()

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not getattr(self.planner, "name", None):
            errs.append(StaticCheckIssue("R12-E15", "planner 未履约", "planner"))
        if self.max_children <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_children <= 0", "max_children"))
        if getattr(self.join, "name", None) not in ("all", "any", "majority", "majority_vote", "reduce"):
            errs.append(StaticCheckIssue("R12-E13", "join 未注册", "join"))
        if not getattr(self.child_spec, "name", None):
            errs.append(StaticCheckIssue("R12-E14", "child_spec 非法（缺 name）", "child_spec"))
        return _report(self.name, tuple(errs), ("llm",), {"parallel_children": self.max_children})

    def compile(self) -> Program:
        """Supervisor = par(子 Agent 扇出) + join 聚合，par 承担并行批与按槽回注。"""
        def spawned(task: DynamicValue) -> ProgramFn:
            factory = self._child_factory(task)
            def child() -> Program:
                def program() -> Program:
                    out = yield Spawn(program_factory=factory)
                    yield Terminate(output=out)
                    return None
                return program()
            return child

        def program() -> Program:
            split_out = yield _invoke("llm", {"messages": [
                {"role": "system", "content": getattr(self.planner, "prompt", "split")}]})
            tasks = _get(_dict(split_out), "tasks", []) or []
            tasks = tasks[:self.max_children]
            if not tasks:
                yield _end([], "join_timeout")
                return
            # par 真实承担 fan-out：子 Spawn 汇成列表一次 yield → R1 并行 → 按槽回注 → 聚合。
            outs = yield from _run_child(par([spawned(t) for t in tasks], join="all")())
            yield _end(self.join.join(list(outs)))
            return None
        return program()

    def _child_factory(self, task: DynamicValue) -> ProgramFn:
        """按 child_spec 与单个 task 构造子 Program 工厂（无参、可重建，D-8）。"""
        spec = self.child_spec

        def child() -> Program:
            factory = getattr(spec, "program_factory", None) or getattr(spec, "build", None)
            if callable(factory):
                out = yield from _run_child(factory(task)())
                yield _end(out)
            else:
                yield _invoke("llm", {"messages": [{"role": "user", "content": task}]})
            return None
        return child


class Handoff:
    """⑧ 角色交接（seq 串接角色 Program；跳数交 Budgets）。"""
    name = "Handoff"; version = "12.0.0"

    def __init__(self, roles: Mapping[str, Arch], *, max_hops: int = 5) -> None:
        self.roles = roles; self.max_hops = max_hops

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.roles:
            errs.append(StaticCheckIssue("R12-E07", "roles 为空", "roles"))
        if self.max_hops <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_hops <= 0", "max_hops"))
        return _report(self.name, tuple(errs), ("llm",), {"hops": self.max_hops})

    def compile(self) -> Program:
        """Handoff = loop_until(单跳角色)，算子承担跳数循环与接力决策。"""
        if not self.roles:
            def empty() -> Program:
                yield _end(None, "no_route")
                return None
            return empty()
        role_order = list(self.roles)
        hop: dict[str, DynamicValue] = {"role": role_order[0]}
        end_reason = "hops_exhausted" if len(role_order) > self.max_hops else ""

        def _step(_feedback: DynamicValue) -> ProgramFn:
            def one_hop() -> Program:
                def program() -> Program:
                    role = str(hop["role"])
                    rp = self.roles[role]
                    if isinstance(rp, Arch):
                        out = yield from _run_child(rp.compile())
                    elif callable(rp):
                        out = yield from _run_child(rp())
                    else:
                        out = yield _invoke("llm", {"name": role})
                    nxt = _get(_dict(out), _X.of("handoff", "role")) or _get(_dict(out), "handoff")
                    if nxt is not None and str(nxt) in self.roles:
                        hop["role"] = str(nxt)
                    else:
                        pos = role_order.index(role)
                        hop["role"] = role_order[pos + 1] if pos + 1 < len(role_order) else None
                    yield Terminate(output=out)
                    return None
                return program()
            return one_hop

        def _done(_out: DynamicValue) -> bool:
            return hop["role"] is None

        return loop_until(_step, done=_done, max_rounds=self.max_hops,
                          reason_fn=lambda: end_reason)()


class Blackboard:
    """⑨ 黑板协作（race 多代理竞速；轮次交 Budgets）。"""
    name = "Blackboard"; version = "12.0.0"

    def __init__(self, agents: Sequence[Arch], *, rounds: int = 3) -> None:
        self.agents = agents; self.rounds = rounds

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.agents:
            errs.append(StaticCheckIssue("R12-E07", "agents 为空", "agents"))
        if self.rounds <= 0:
            errs.append(StaticCheckIssue("R12-E17", "rounds <= 0", "rounds"))
        seen_writes: dict[str, int] = {}
        for a in self.agents:
            for key in getattr(a, "writes", ()) or ():
                seen_writes[str(key)] = seen_writes.get(str(key), 0) + 1
        for key, count in seen_writes.items():
            if count > 1:
                errs.append(StaticCheckIssue("R12-E16", f"共享键 {key} 无仲裁", key))
        return _report(self.name, tuple(errs), ("llm",), {"rounds": self.rounds})

    def compile(self) -> Program:
        """Blackboard = loop_until(轮次) + par(多代理并行派发)，算子承担并行与循环。"""
        rk = _X.of("bb", "round")
        bk = _X.of("bb", "board")

        def spawn_arm(agent: Arch, board: Mapping[str, DynamicValue]) -> ProgramFn:
            def arm() -> Program:
                def program() -> Program:
                    result = yield Spawn(program_factory=self._bb_agent(agent, board))
                    yield Terminate(output=result)
                    return None
                return program()
            return arm

        def _step(feedback: DynamicValue) -> ProgramFn:
            def one_round() -> Program:
                def program() -> Program:
                    board = dict(feedback) if isinstance(feedback, Mapping) else {}
                    merged = yield from _run_child(
                        par([spawn_arm(a, board) for a in self.agents], join="all")())
                    new_board: dict[str, DynamicValue] = dict(board)
                    # 首写胜出：par 按 agents 声明序保序 → setdefault 即首个胜出仲裁。
                    for value in (merged if isinstance(merged, list) else [merged]):
                        writes = _dict(_get(_dict(value), "writes", {}))
                        for key, val in writes.items():
                            new_board.setdefault(key, val)
                    new_board[rk] = int(new_board.get(rk, 0)) + 1
                    new_board[bk] = dict(new_board)
                    yield Terminate(output=new_board)
                    return None
                return program()
            return one_round

        def _done(out: DynamicValue) -> bool:
            return int(_dict(out).get(rk, 0)) >= self.rounds

        return loop_until(_step, done=_done, max_rounds=self.rounds)()
    def _bb_agent(self, agent: Arch, board: Mapping[str, DynamicValue]) -> ProgramFn:
        """按单个 agent 与共享 board 构造子 Program 工厂。"""
        def child() -> Program:
            factory = agent.compile if isinstance(agent, Arch) else agent
            out = yield from _run_child(factory())
            # 把子 Agent 输出归一为 write 信封（约定 {"writes": {...}}）。
            writes = _dict(_get(_dict(out), "writes", {})) or {"result": out}
            yield _end({"writes": writes})
            return None
        return child


class Graph:
    """⑩ 声明式图（.node/.edge/.exit/.compile；node 用 seq 串接）。"""
    name = "Graph"; version = "12.0.0"

    def __init__(self) -> None:
        self._nodes: dict[str, ProgramFn] = {}
        self._inputs: dict[str, tuple[str, ...]] = {}   # node id → 声明的 inputs
        self._outputs: dict[str, tuple[str, ...]] = {}  # node id → 声明的 outputs
        self._edges: list[_Edge] = []
        self._exits: set[str] = set()

    def node(self, id: str, program: ProgramFn | Arch, *,
             inputs: tuple[str, ...] = (), outputs: tuple[str, ...] = ()) -> Graph:
        # 保存 compile 工厂本身（不是一次性 generator），保证二次 compile/resume 幂等（D-8）。
        self._nodes[id] = program.compile if isinstance(program, Arch) else program
        self._inputs[id] = tuple(inputs)
        self._outputs[id] = tuple(outputs)
        return self

    def edge(self, src: str, dst: str, *, cond: Callable[[AgentState], str | None] | None = None) -> Graph:
        self._edges.append(_Edge(src=src, dst=dst, cond=cond))
        return self

    def exit(self, id: str) -> Graph:
        self._exits.add(id); return self

    def static_check(self) -> StaticCheckReport:
        errs: list[StaticCheckIssue] = []
        known = set(self._nodes) | self._exits
        # E04：恰好一个 exit（0 或多个都拒绝）。
        if len(self._exits) != 1:
            errs.append(StaticCheckIssue("R12-E04", f"exit 数量为 {len(self._exits)}（须恰好 1 个）", "exit"))
        # E01：悬空边。
        for e in self._edges:
            if e.src not in known or e.dst not in known:
                errs.append(StaticCheckIssue("R12-E01", f"悬空边 {e.src}->{e.dst}", e.src))
        # E06：同一 src 的多条 cond 边目标重复。
        by_src: dict[str, list[str]] = {}
        for e in self._edges:
            if e.cond is not None:
                by_src.setdefault(e.src, []).append(e.dst)
        for src, targets in by_src.items():
            if len(set(targets)) != len(targets):
                errs.append(StaticCheckIssue("R12-E06", f"cond 目标重复于 {src}", src))
        # E05：cond 返回未声明节点 id——此处以最小 AgentState 探测用户 cond 的返回；
        # 仅收集声明期可判定的字符串目标，运行期才知道的 cond 异常被隔离不误报。
        for e in self._edges:
            if e.cond is None:
                continue
            try:
                probe = e.cond(AgentState())
            except Exception:
                continue  # 运行期才知道的 cond 不在此误报
            if isinstance(probe, str) and probe and probe not in known:
                errs.append(StaticCheckIssue("R12-E05", f"cond 目标非法 {probe}", e.src))
        # E02：环（DFS 检测，回边即报）。
        if self._has_cycle():
            errs.append(StaticCheckIssue("R12-E02", "图中存在环", "graph"))
        # E03：从 entry 不可达的节点。
        entry = self._entry()
        if entry is not None:
            reachable = self._reachable(entry)
            for nid in self._nodes:
                if nid not in reachable:
                    errs.append(StaticCheckIssue("R12-E03", f"不可达节点 {nid}", nid))
        return _report(self.name, tuple(errs), ("llm",))

    def compile(self) -> Program:
        """Graph = loop_until(节点遍历)，算子承担图行走循环与环检测。"""
        entry = self._entry()
        exit_id = next(iter(self._exits)) if self._exits else None
        if entry is None or exit_id is None:
            def invalid() -> Program:
                yield _end(None, "graph_invalid")
                return None
            return invalid()

        cur: dict[str, DynamicValue] = {"id": entry, "reason": "", "stop": False}
        visited: set[str] = set()
        budget = max(1, len(self._nodes) + len(self._edges))

        def _step(_feedback: DynamicValue) -> ProgramFn:
            def one_node() -> Program:
                def program() -> Program:
                    node = cur["id"]
                    if node is None or node in visited or node not in self._nodes:
                        cur["reason"] = "cycle" if node in visited else "graph_invalid"
                        cur["stop"] = True
                        yield Terminate(output=None)
                        return None
                    visited.add(node)
                    out = yield from _run_child(self._nodes[str(node)]())
                    if node == exit_id:
                        cur["stop"] = True
                    else:
                        nxt = self._next(str(node), _dict(out))
                        if nxt is None or nxt not in self._nodes:
                            cur["stop"] = True
                        else:
                            cur["id"] = nxt
                    yield Terminate(output=out)
                    return None
                return program()
            return one_node

        def _done(_out: DynamicValue) -> bool:
            return bool(cur["stop"])

        return loop_until(_step, done=_done, max_rounds=budget,
                          reason_fn=lambda: str(cur["reason"]))()

    # ---- 内部纯函数（不进公共 API） ----
    def _entry(self) -> str | None:
        """入口 = 第一个声明的 node（保持向后兼容）；无边时即该节点。"""
        return next(iter(self._nodes), None)

    def _adjacency(self) -> dict[str, list[str]]:
        """构造邻接表：src → 目标列表（cond 边取其 dst；_next 另行按 state 选）。"""
        adj: dict[str, list[str]] = {}
        for e in self._edges:
            adj.setdefault(e.src, []).append(e.dst)
        return adj

    def _next(self, cur: str, state: Mapping[str, DynamicValue]) -> str | None:
        """按 cond(state) 选下一节点：返回首个非 None 的条件目标；否则走无条件 dst。"""
        outgoing = [e for e in self._edges if e.src == cur]
        for e in outgoing:
            if e.cond is not None:
                # cond 签名收 AgentState；此处回注是 dict，构造只读 State 视图供其判定。
                try:
                    target = e.cond(AgentState(scratch=dict(state)))
                except Exception:  # cond 异常按无匹配处理，走无条件边
                    target = None
                if target is not None:
                    return target
        for e in outgoing:
            if e.cond is None:
                return e.dst
        return None

    def _reachable(self, start: str) -> set[str]:
        """从 start 出发按邻接表可达的节点集合。"""
        adj = self._adjacency()
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, ()))
        return seen

    def _has_cycle(self) -> bool:
        """DFS 三色标记检测有向环。"""
        adj = self._adjacency()
        color: dict[str, int] = {}  # 0=未访问 1=在栈 2=完成

        def visit(node: str) -> bool:
            color[node] = 1
            for nxt in adj.get(node, ()):
                if color.get(nxt, 0) == 1:
                    return True
                if color.get(nxt, 0) == 0 and visit(nxt):
                    return True
            color[node] = 2
            return False

        return any(color.get(node, 0) == 0 and visit(node) for node in self._nodes)
