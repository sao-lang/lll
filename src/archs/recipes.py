"""10 内置架构 = A 类算子的预设配方（R12 §6，规格 P6）。

每个架构实现 Arch 协议（name/version/static_check/compile），compile() 用
compose 的 A 类算子（seq/par/branch/race/loop/...）组合出 Program；R1 只认
Program，架构知识全在装配期。构造签名对齐 R12 §4「对外 API 不变」。

预算（max_turns/max_rounds/max_hops）是 Budgets 维度（R1 强制扣减），架构
generator 不自数轮次（铁律）；这里把它们记入 static_check 的 budgets_resolved
供装配期核对，compile 产出的 Program 不含自数循环。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from compose.program import ProgramFn, branch, loop, par, race, seq
from compose import ToolSpec
from runtime import AgentState, Invoke, Program, Terminate

from .base import Arch, JoinStrategy, MajorityVote, StaticCheckIssue, StaticCheckReport

__all__ = [
    "ToolUse", "ReAct", "PlanExecute", "Reflection", "Router",
    "RouterSkill", "Supervisor", "Handoff", "Blackboard", "Graph",
]


def _leaf(port: str, name: str) -> ProgramFn:
    """最小叶子 Program 工厂：yield 一个 port Effect + Terminate（装配期纯配方）。

    返回 ProgramFn（Callable[[], Program]），因为 A 类算子在工厂层做装配期变换、
    resume 靠工厂重建（D-8）。
    """
    def program() -> Program:
        yield Invoke(port=port).with_payload(name=name)
        yield Terminate(output=None)
        return None
    return program


def _report(name: str, errors: tuple[StaticCheckIssue, ...], ports: tuple[str, ...],
            budgets: dict[str, int | float] | None = None) -> StaticCheckReport:
    return StaticCheckReport(errors=errors, arch=name, version="12.0.0",
                             ports_required=ports, budgets_resolved=budgets or {})


def _seq_all(leaves: list[ProgramFn]) -> ProgramFn:
    """把全部叶子用 seq 串接（保留所有中间项；seq 只收 2 个，故逐个 reduce）。"""
    if not leaves:
        leaves = [_leaf("llm", "noop")]
    cur = leaves[0]
    for nxt in leaves[1:]:
        cur = seq(cur, nxt)
    return cur


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
        leaves: list[ProgramFn] = [_leaf("tool", t.name) for t in self.tools] or [_leaf("tool", "noop")]
        recipe = par(leaves) if self.parallel else _seq_all(leaves)
        return recipe()


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
        step = seq(_leaf("llm", "reason"), _leaf("tool", "act"))
        return loop(step, cond=lambda s: False)()


class PlanExecute:
    """③ 规划-执行状态机（seq(plan, execute, [replan])）。"""
    name = "PlanExecute"; version = "12.0.0"

    def __init__(self, planner: Any, executor: Any, *, replanner: Any | None = None) -> None:
        self.planner = planner; self.executor = executor; self.replanner = replanner

    def static_check(self) -> StaticCheckReport:
        errs = []
        for role, obj in (("planner", self.planner), ("executor", self.executor)):
            if obj is None or not getattr(obj, "name", None):
                errs.append(StaticCheckIssue("R12-E15", f"{role} 未履约", role))
        return _report(self.name, tuple(errs), ("llm",))

    def compile(self) -> Program:
        return seq(_leaf("llm", "plan"), _leaf("llm", "execute"))()


class Reflection:
    """④ 起草-反思-修订（seq(draft, reflect, revise)；轮次交 Budgets）。"""
    name = "Reflection"; version = "12.0.0"

    def __init__(self, reflector: Any, *, max_rounds: int = 3) -> None:
        self.reflector = reflector; self.max_rounds = max_rounds

    def static_check(self) -> StaticCheckReport:
        errs = []
        if self.max_rounds <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_rounds <= 0", "max_rounds"))
        if not getattr(self.reflector, "name", None):
            errs.append(StaticCheckIssue("R12-E15", "reflector 未履约", "reflector"))
        return _report(self.name, tuple(errs), ("llm",), {"rounds": self.max_rounds})

    def compile(self) -> Program:
        return _seq_all([_leaf("llm", "draft"), _leaf("llm", "reflect"), _leaf("llm", "revise")])()


class Router:
    """⑤ 意图路由（branch(fn, arms, default)）。"""
    name = "Router"; version = "12.0.0"

    def __init__(self, classifier: Any, arms: dict[str, Any], *,
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
        arms: dict[str, ProgramFn] = {k: _leaf("llm", k) for k in self.arms} or {"_": _leaf("llm", "_")}
        default = self.default or next(iter(arms))
        return branch(lambda s: default, arms)()


class RouterSkill(Router):
    """⑥ 技能路由（Router 变体，routes 可含 SkillSpec）。"""
    name = "RouterSkill"

    def __init__(self, classifier: Any, routes: dict[str, Any], default: str) -> None:
        super().__init__(classifier, routes, default=default)


class Supervisor:
    """⑦ 监督者-子代理（par(planner, children)；子数交 Budgets）。"""
    name = "Supervisor"; version = "12.0.0"

    def __init__(self, planner: Any, child_spec: Any, *, max_children: int = 8,
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
        return par([_leaf("llm", "plan"), _leaf("llm", "child")])()


class Handoff:
    """⑧ 角色交接（seq 串接角色 Program；跳数交 Budgets）。"""
    name = "Handoff"; version = "12.0.0"

    def __init__(self, roles: dict[str, Any], *, max_hops: int = 5) -> None:
        self.roles = roles; self.max_hops = max_hops

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.roles:
            errs.append(StaticCheckIssue("R12-E07", "roles 为空", "roles"))
        if self.max_hops <= 0:
            errs.append(StaticCheckIssue("R12-E17", "max_hops <= 0", "max_hops"))
        return _report(self.name, tuple(errs), ("llm",), {"hops": self.max_hops})

    def compile(self) -> Program:
        leaves = [_leaf("llm", r) for r in self.roles] or [_leaf("llm", "noop")]
        return _seq_all(leaves)()


class Blackboard:
    """⑨ 黑板协作（race 多代理竞速；轮次交 Budgets）。"""
    name = "Blackboard"; version = "12.0.0"

    def __init__(self, agents: list[Any], *, rounds: int = 3) -> None:
        self.agents = agents; self.rounds = rounds

    def static_check(self) -> StaticCheckReport:
        errs = []
        if not self.agents:
            errs.append(StaticCheckIssue("R12-E07", "agents 为空", "agents"))
        if self.rounds <= 0:
            errs.append(StaticCheckIssue("R12-E17", "rounds <= 0", "rounds"))
        return _report(self.name, tuple(errs), ("llm",), {"rounds": self.rounds})

    def compile(self) -> Program:
        leaves = [_leaf("llm", f"agent{i}") for i in range(len(self.agents))] or [_leaf("llm", "a")]
        return race(leaves)()


class Graph:
    """⑩ 声明式图（.node/.edge/.exit/.compile；node 用 seq 串接）。"""
    name = "Graph"; version = "12.0.0"

    def __init__(self) -> None:
        self._nodes: dict[str, ProgramFn] = {}
        self._edges: list[tuple[str, str]] = []
        self._exits: set[str] = set()

    def node(self, id: str, program: ProgramFn | Arch, *,
             inputs: tuple[str, ...] = (), outputs: tuple[str, ...] = ()) -> "Graph":
        if isinstance(program, Arch):
            # 保存 compile 工厂本身（不是一次性 generator），保证二次 compile/resume 幂等（D-8）。
            self._nodes[id] = program.compile
        else:
            self._nodes[id] = program
        return self

    def edge(self, src: str, dst: str, *, cond: Callable[[AgentState], str | None] | None = None) -> "Graph":
        self._edges.append((src, dst)); return self

    def exit(self, id: str) -> "Graph":
        self._exits.add(id); return self

    def static_check(self) -> StaticCheckReport:
        errs = []
        for src, dst in self._edges:
            if src not in self._nodes or (dst not in self._nodes and dst not in self._exits):
                errs.append(StaticCheckIssue("R12-E01", f"悬空边 {src}->{dst}", src))
        return _report(self.name, tuple(errs), ("llm",))

    def compile(self) -> Program:
        nodes = list(self._nodes.values()) or [_leaf("llm", "start")]
        # 用 _seq_all 串接全部节点（不重复首尾；单节点只跑一次）。
        return _seq_all(nodes)()
