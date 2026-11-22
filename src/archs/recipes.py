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

import asyncio
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from compose import ToolSpec
from compose.program import ProgramFn
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
        return _report(self.name, tuple(errs), ("tool",))

    def compile(self) -> Program:
        def program() -> Program:
            calls = [{"name": t.name, "call": {"call_id": t.name, "name": t.name, "args": {}}}
                     for t in self.tools]
            if not calls:
                yield _end([], "tool_all_failed")
                return
            if self.parallel:
                # 一次 batch Invoke，解释器展开为多个独立 tool Effect；回填 list[data]。
                results = yield _batch("tool", calls)
                yield _end(list(results) if isinstance(results, list) else [results])
            else:
                # 串行：逐个调用，收集每次回填的 data。
                results_list: list[DynamicValue] = []
                for call in calls:
                    data = yield _invoke("tool", call)
                    results_list.append(data)
                yield _end(results_list)
            return None
        return program()


class ReAct:
    """② 推理-行动自循环（loop 承载；轮次交 Budgets）。"""
    name = "ReAct"; version = "12.0.0"

    def __init__(self, tools: list[ToolSpec], *, llm: str | None = None, max_turns: int = 30) -> None:
        self.tools = tools; self.llm = llm; self.max_turns = max_turns

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.tools:
            errs.append(StaticCheckIssue("R12-E07", "tools 为空", "tools"))
        if self.max_turns <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_turns <= 0", "max_turns"))
        return _report(self.name, tuple(errs), ("llm", "tool"), {"turns": self.max_turns})

    def compile(self) -> Program:
        def program() -> Program:
            messages: list[DynamicValue] = []
            payload: dict[str, DynamicValue] = {
                "messages": messages,
                "tools": [t.model_dump() if hasattr(t, "model_dump") else t for t in self.tools],
            }
            if self.llm is not None:
                payload["model"] = self.llm
            for _ in range(self.max_turns):  # 防御上限；真实轮次约束由 R1 Budgets.turns 兜底
                llm_out = yield _invoke("llm", payload)
                assistant = _dict(llm_out)
                calls = _tool_calls(llm_out)
                # 把本轮 assistant 消息追加进 messages，下一轮带上完整历史。
                messages.append({"role": "assistant", "content": _get(assistant, "content", ""),
                                 "tool_calls": calls})
                payload["messages"] = list(messages)
                if not calls:
                    yield _end(_get(assistant, "content"))
                    return
                # 批量调工具，回填 list[data]；作为 tool 消息注入历史。
                tool_results = yield _batch("tool", calls)
                results = tool_results if isinstance(tool_results, list) else [tool_results]
                for call, data in zip(calls, results):
                    messages.append({"role": "tool", "name": _get(call, "name", ""),
                                     "content": data})
                    payload["messages"] = list(messages)
            yield _end(messages[-1] if messages else None, "turns_exhausted")
            return None
        return program()


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
        def program() -> Program:
            # 规划：LLM 产出步骤（约定回注 {"steps": [...]} 或 {"content": ...}）。
            plan_out = yield _invoke("llm", {"messages": [
                {"role": "system", "content": getattr(self.planner, "prompt", "plan")}]})
            steps = self._parse_steps(plan_out)
            last: DynamicValue = None
            replan_budget = self.max_replans if hasattr(self, "max_replans") else len(steps) + 1
            for step in steps:
                # 执行：按步骤构造 tool/llm 调用（约定 step={"port","payload"}）。
                step_map = _dict(step)
                port = str(_get(step_map, "port", "tool"))
                payload = _dict(_get(step_map, "payload", {}))
                out = yield _invoke(port, payload)
                last = out
                failed = bool(_get(_dict(out), "ok", True) is False or _get(_dict(out), "error"))
                if failed and self.replanner is not None and replan_budget > 0:
                    replan_out = yield _invoke("llm", {"messages": [
                        {"role": "system", "content": getattr(self.replanner, "prompt", "replan")},
                        {"role": "user", "content": {"steps": steps, "failed": step, "last": out}},
                    ]})
                    steps = self._parse_steps(replan_out)
                    replan_budget -= 1
            yield _end(last, "replan_exhausted" if replan_budget <= 0 else "")
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
        def program() -> Program:
            # 起草：LLM 产出 draft 文本。
            draft_out = yield _invoke("llm", {"messages": [
                {"role": "system", "content": "draft"}]})
            best = _get(_dict(draft_out), "content", "") or ""
            for _ in range(self.max_rounds):  # 防御上限；真实轮次由 R1 Budgets.rounds 兜底
                # 反思：reflector 产 critique（约定 {"passed":bool,"comments":...}）。
                critique_out = yield _invoke("llm", {"messages": [
                    {"role": "system", "content": getattr(self.reflector, "prompt", "critique")},
                    {"role": "user", "content": best},
                ]})
                critique = _dict(critique_out)
                if bool(_get(critique, "passed", False)):
                    yield _end(best)
                    return
                # 修订：按 critique 改进 draft。
                revise_out = yield _invoke("llm", {"messages": [
                    {"role": "system", "content": "revise"},
                    {"role": "user", "content": {"draft": best, "critique": critique}},
                ]})
                best = _get(_dict(revise_out), "content", best) or best
            yield _end(best, "rounds_exhausted")
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
        def program() -> Program:
            intent = self._recognize()
            label, conf = intent.label, intent.confidence
            target = label if conf >= self.min_confidence and label in self.arms else self.default
            if target is None:
                yield _end(None, "no_route")
                return
            arm = self.arms[target]
            # arm 可是 Arch/任意带 compile() 的对象，或直接是 ProgramFn。
            factory: ProgramFn = arm.compile if hasattr(arm, "compile") else arm  # type: ignore[assignment]
            out = yield from _run_child(factory())
            yield _end(out)
            return None
        return program()

    def _recognize(self) -> IntentResult:
        """调 classifier 得 IntentResult。

        注入 IntentStrategy 时以默认文本走 recognize()（异步协议，此处取协程结果）；
        注入 Callable 时直接调用；未注入时按 label 为空走兜底。
        """
        cls = self.classifier
        if cls is None:
            return IntentResult(label="", confidence=0.0)
        if isinstance(cls, IntentStrategy):
            result: DynamicValue = asyncio.run(cls.recognize("", None))  # type: ignore[arg-type]
        else:
            raw: DynamicValue = cls("")  # Callable[[str], ...]
            result = raw if not hasattr(raw, "__await__") else asyncio.run(raw)
        if isinstance(result, IntentResult):
            return result
        data = _dict(result)
        return IntentResult(label=str(_get(data, "label", "")),
                            confidence=float(_get(data, "confidence", 0.0)))


class RouterSkill(Router):
    """⑥ 技能路由（Router 变体，routes 可含 SkillSpec）。"""
    name = "RouterSkill"

    def __init__(self, classifier: IntentStrategy | Callable[..., IntentResult] | None,
                 routes: Mapping[str, Arch | SkillSpec], default: str) -> None:
        # 签名按 R12 §4 保留 routes 名；值类型与基类 arms 一致，
        # 只读映射可直接向上传递。
        super().__init__(classifier, routes, default=default)

    def compile(self) -> Program:
        def program() -> Program:
            intent = self._recognize()
            route = self.arms.get(intent.label) or self.arms.get(self.default or "")
            if route is None:
                yield _end(None, "no_route")
                return
            # SkillSpec：先加载技能正文（R5 SkillPort 履约），再执行下游。
            if isinstance(route, SkillSpec):
                yield _invoke("skill", {"name": route.name, "part": "body"})
                downstream = getattr(route, "downstream", None)
                if downstream is not None:
                    factory = downstream.compile if isinstance(downstream, Arch) else downstream
                    out = yield from _run_child(factory())
                    yield _end(out)
                    return
            elif isinstance(route, Arch):
                out = yield from _run_child(route.compile())
                yield _end(out)
                return
            else:  # ProgramFn
                out = yield from _run_child(route())
                yield _end(out)
                return
            yield _end(_get(_dict(route), "name", intent.label))
            return None
        return program()


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
        return _report(self.name, tuple(errs), ("llm",), {"parallel_children": self.max_children})

    def compile(self) -> Program:
        def program() -> Program:
            # 拆分任务：LLM 产出 {"tasks": [...]}。
            split_out = yield _invoke("llm", {"messages": [
                {"role": "system", "content": getattr(self.planner, "prompt", "split")}]})
            tasks = _get(_dict(split_out), "tasks", []) or []
            tasks = tasks[:self.max_children]  # 并发上限（Budgets.parallel_children 再校验）
            if not tasks:
                yield _end([], "join_timeout")
                return
            # 并行 Spawn 子 Agent：每个子 Program 由 child_spec 构造。
            spawns = [Spawn(program_factory=self._child_factory(t)) for t in tasks]
            results = yield spawns  # type: ignore[misc]  # 解释器支持 yield list[Effect]（并行声明），回填 list[data]
            values = results if isinstance(results, list) else [results]
            yield _end(self.join.join(list(values)))
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
        def program() -> Program:
            if not self.roles:
                yield _end(None, "no_route")
                return
            role_order = list(self.roles)             # 声明顺序即默认接力顺序
            role = role_order[0]                       # 起始角色
            last: DynamicValue = None
            for _ in range(self.max_hops):  # 防御上限；真实跳数由 R1 Budgets.hops 兜底
                role_program = self.roles[role]
                if isinstance(role_program, Arch):
                    out = yield from _run_child(role_program.compile())
                elif callable(role_program):
                    out = yield from _run_child(role_program())
                else:
                    # 非 Program 值（如占位对象/角色名）：发一个具名 Invoke 表示交接到达该角色。
                    out = yield _invoke("llm", {"name": role})
                last = out
                # 交接目标从回注读（x-arch.handoff.role / handoff）；无显式目标则按声明顺序接力。
                nxt = _get(_dict(out), _X.of("handoff", "role")) or _get(_dict(out), "handoff")
                if nxt is not None and str(nxt) in self.roles:
                    role = str(nxt)
                else:
                    pos = role_order.index(role)
                    if pos + 1 >= len(role_order):     # 已是末位角色：交接完成
                        break
                    role = role_order[pos + 1]
            yield _end(last, "hops_exhausted" if len(role_order) > self.max_hops else "")
            return None
        return program()


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
        return _report(self.name, tuple(errs), ("llm",), {"rounds": self.rounds})

    def compile(self) -> Program:
        def program() -> Program:
            board: dict[str, DynamicValue] = {}
            for _ in range(self.rounds):  # 防御上限；真实轮次由 R1 Budgets.rounds 兜底
                # 并行派发子 Agent（每个 agent 一个 Spawn），传当前 board。
                spawns = [Spawn(program_factory=self._bb_agent(a, board)) for a in self.agents]
                results = yield spawns  # type: ignore[misc]  # 解释器支持 yield list[Effect]（并行声明）
                values = results if isinstance(results, list) else [results]
                # 合并各 agent 的写入（约定回注 {"writes": {...}}）；冲突键按首个胜出仲裁。
                for value in values:
                    writes = _dict(_get(_dict(value), "writes", {}))
                    for key, val in writes.items():
                        board.setdefault(key, val)
                board[_X.of("bb", "round")] = int(board.get(_X.of("bb", "round"), 0)) + 1
            board[_X.of("bb", "board")] = dict(board)
            yield _end(board)
            return None
        return program()

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
        def program() -> Program:
            entry = self._entry()
            exit_id = next(iter(self._exits)) if self._exits else None
            if entry is None or exit_id is None:
                yield _end(None, "graph_invalid")
                return
            cur: str | None = entry
            visited: set[str] = set()
            state_out: DynamicValue = None
            for _ in range(max(1, len(self._nodes) + len(self._edges))):  # 防御上限（static_check 已拦截环）
                if cur is None or cur in visited or cur not in self._nodes:
                    yield _end(None, "cycle" if cur in visited else "graph_invalid")
                    return
                visited.add(cur)
                # 执行当前节点；exit 节点也执行一次（单节点图 entry==exit 时保证节点被跑）。
                state_out = yield from _run_child(self._nodes[cur]())
                if cur == exit_id:
                    break
                nxt = self._next(cur, _dict(state_out))
                if nxt is None or nxt not in self._nodes:
                    break
                cur = nxt
            yield _end(state_out)
            return None
        return program()

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
