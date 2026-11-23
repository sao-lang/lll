"""A 类 10 组合算子（规格 §2.3 / D-4）：装配期 Program → Program 纯变换。

设计约束（R1 不改前提下）：
- 装配期纯变换，不改原 Program（compile 幂等）；
- 必须是纯函数：运行期状态进 State.scratch 的 x-* 键，禁止算子实例持有流程进度
  （resume 靠 program_factory 重建，D-8）；
- 轮次/跳数交 Budgets，架构 generator 内禁自数；
- State 只读，改状态靠 state.set 产生新对象（局部持有，不写 R1 fold）；
- gate 是唯一可 yield Suspend 的 A 类算子（D-6）。

算子间数据传递：_run_child 拦截子 Program 的 Terminate 提取 output，写入局部
State 的 x-compose.* scratch 键；下游算子启动前把 x-* 键合并进回注 State
（R1 fold 不感知 x-*，故由组合层自身承担 carry，纯函数、resume 可重建）。
"""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from enum import Enum
from typing import Final

from runtime import (
    AgentState,
    DynamicValue,
    Effect,
    Invoke,
    Program,
    Suspend,
    Terminate,
)

__all__ = [
    "JoinMode",
    "ProgramFn",
    "branch",
    "fallback",
    "gate",
    "loop",
    "map_effect",
    "map_state",
    "par",
    "race",
    "retry",
    "seq",
]

# Program 是 generator 对象；实际 program 由工厂函数（每次调用产出新 generator）提供，
# resume 靠 program_factory 重建（D-8）。算子在该函数层面做装配期纯变换。
type ProgramFn = Callable[[], Program]


class _Keys:
    """算子私有 scratch 键（x-* 前缀，D-2 扩展键约定），集中定义禁用魔法字符串。"""
    PREFIX: Final[str] = "x-compose."
    SEQ_OUTPUT: Final[str] = PREFIX + "seq.output"
    ATTEMPT: Final[str] = PREFIX + "attempt"
    FAIL_FLAG: Final[str] = PREFIX + "fail"
    LOOP_ROUND: Final[str] = PREFIX + "loop.round"
    RETRY_COUNT: Final[str] = PREFIX + "retry.count"


class JoinMode(str, Enum):
    """par 并行结果的聚合策略（有限取值集合，禁用裸字符串比较）。"""
    ALL = "all"
    ANY = "any"
    MAJORITY = "majority"


def _as_state(value: DynamicValue) -> AgentState:
    """把回注值归一为可读 State 视图（真实 R1 回注 Result.data，可能是 dict/str/None）。

    分支：
        - 本身就是 AgentState：原样返回；
        - dict：按其键构造 State（测试/上层自定义回注）；
        - 其它（str/None）：返回空 State（组合层局部进度另由 _carry_x 维护）。

    Args:
        value: Effect 的回注值。

    Returns:
        可安全读取的 AgentState 视图。
    """
    if isinstance(value, AgentState):
        return value
    if isinstance(value, dict):
        return AgentState().set("scratch", value) if value else AgentState()
    return AgentState()


def _carry_x(into: AgentState, source: AgentState) -> AgentState:
    """把 source 的 x-* scratch 键合并进 into（source 覆盖，组合层自持的进度不被 fold 回注冲掉）。

    Args:
        into: 本轮回注 State（保留其托管字段/messages/budget）。
        source: 携带算子进度的 State（局部持有）。

    Returns:
        合并后的新 State（不可变）。
    """
    merged = dict(into.scratch)
    for key, value in source.scratch.items():
        if str(key).startswith(_Keys.PREFIX):
            merged[key] = value
    return into.with_(scratch=merged)


def _run_child(
    child: ProgramFn,
    state: AgentState,
) -> Generator[Effect, AgentState, tuple[DynamicValue, AgentState]]:
    """驱动子 Program：双向转发 Effect，拦截 Terminate 返回其 output。

    分支：
        - 子 yield Terminate：拦截（不转发给 R1，避免提前结束整个 run），返回 output 与最新 State；
        - 子 yield 其他 Effect：转发给外层执行，回注 State 经 x-* 合并后 send 回子 Program；
        - 子 StopIteration（无 Terminate 直接 return）：output 取 State.output。
    """
    gen = child()
    try:
        effect = next(gen)
        while True:
            if isinstance(effect, Terminate):
                return effect.output, state
            # 真实 R1 回注的是 Result.data（DynamicValue），原样送回子 generator；
            # 组合层进度由局部 state 自持，不依赖回注值形态（D-8）。
            sent: DynamicValue = yield effect
            if isinstance(effect, Invoke):
                carried = {k: v for k, v in effect.payload.items()
                           if str(k).startswith("x-")}
                if carried:
                    state = state.with_(scratch={**dict(state.scratch), **carried})
            effect = gen.send(sent)
    except StopIteration:
        return state.output, state


def _as_program(child: ProgramFn | Effect) -> ProgramFn:
    """把 Effect 常量包装为 Program（便于声明式 arm）。"""
    if isinstance(child, Effect):
        def constant() -> Generator[Effect, AgentState, None]:
            yield child
        return constant
    return child


def seq(a: ProgramFn, b: ProgramFn) -> ProgramFn:
    """顺序组合：先跑 a 到 Terminate，output 写入 x-* 后跑 b，b 的 output 为整体产出。

    Args:
        a: 前置 Program。
        b: 后置 Program，从 scratch 读取 a 的产出。

    Returns:
        新 Program（不改 a/b）。
    """
    def program() -> Generator[Effect, AgentState, None]:
        out_a, state = yield from _run_child(a, AgentState())
        state = state.set(_Keys.SEQ_OUTPUT, out_a)
        out_b, state = yield from _run_child(b, state)
        yield Terminate(output=out_b)
    return program


def _hashable(value: DynamicValue) -> DynamicValue:
    """把不可哈希的回注值归一为可哈希键（多数票统计用，纯函数）。

    Args:
        value: 任意 DynamicValue 回注值。

    Returns:
        本身（标量）或其字符串形式（容器）。
    """
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def _join_outputs(join: str, outputs: list[DynamicValue]) -> DynamicValue:
    """按 join 策略聚合并行子 Program 的 output（纯函数，墙钟不介入）。

    分支：
        - any：首个非 None；majority：多数票（平票取首现，确定性）；
        - all 及其它：按槽位顺序返回完整 list。

    Args:
        join: 聚合策略名（all/any/majority）。
        outputs: 按槽位顺序排列的子 output。

    Returns:
        聚合后的 output。
    """
    if join == JoinMode.ANY.value:
        return next((value for value in outputs if value is not None), None)
    if join == JoinMode.MAJORITY.value and outputs:
        keys = [_hashable(value) for value in outputs]
        counts: dict[DynamicValue, int] = {}
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
        winner = max(counts, key=lambda k: (counts[k], -keys.index(k)))
        return outputs[keys.index(winner)]
    return list(outputs)


def par(items: Sequence[ProgramFn], *, join: str = "all") -> ProgramFn:
    """扇出并行：每轮把各子 Program 的 Effect 汇成一列表一次 yield（走 R1 列表并行），按 join 聚合。

    R1 interpreter 认 yield list[Effect] 为并行批（interpreter.py:153-162），runner
    gather 并发执行并按序回注 list（runner.py:515-526）。本算子把该机制暴露给子 Program：
    所有子 Program 同步推进（lockstep），Terminate 的子取其 output，全部完成后聚合。

    Args:
        items: 待并行的 Program 序列（每个产出独立 generator）。
        join: 聚合策略（all/any/majority）。

    Returns:
        新 Program（不改 items，纯函数可重建）。
    """
    def program() -> Generator[Effect, DynamicValue, None]:
        gens = [child() for child in items]
        outputs: dict[int, DynamicValue] = {}
        # pending[i] 是槽位 i 下一次待并行下发的 Effect（Terminate 已拦截记账）。
        pending: dict[int, Effect] = {}
        for i, gen in enumerate(gens):
            try:
                effect = next(gen)
            except StopIteration:
                outputs[i] = None
                continue
            if isinstance(effect, Terminate):
                outputs[i] = effect.output
            else:
                pending[i] = effect
        while pending:
            slots = list(pending.keys())
            batch = [pending[slot] for slot in slots]
            # 一次 yield 整个 Effect 列表 → R1 并行执行 → 按序回注 list。
            injected_raw: DynamicValue = yield batch  # type: ignore[misc]
            values = (
                list(injected_raw)
                if isinstance(injected_raw, (list, tuple))
                else [injected_raw] * len(slots)
            )
            for slot, value in zip(slots, values):
                pending.pop(slot)
                try:
                    effect = gens[slot].send(value)
                except StopIteration:
                    outputs[slot] = None
                    continue
                if isinstance(effect, Terminate):
                    outputs[slot] = effect.output
                else:
                    pending[slot] = effect
        ordered = [outputs.get(i) for i in range(len(gens))]
        yield Terminate(output=_join_outputs(join, ordered))
    return program


def branch(
    fn: Callable[[AgentState], str],
    arms: dict[str, ProgramFn],
    default: str | None = None,
) -> ProgramFn:
    """条件分流：fn(state) 返回 key 选 arms[key]，缺失走 default。

    Args:
        fn: 状态 → arm key 的纯函数。
        arms: key → Program 映射。
        default: key 缺失时的兜底 arm。

    Returns:
        新 Program。
    """
    def program() -> Generator[Effect, AgentState, None]:
        # 首次推进拿到初始 state（yield 一个空操作 Invoke 触发回注）
        state = yield Invoke(port="state").with_payload(op="read")
        state = _as_state(state)
        key = fn(state)
        target = arms.get(key) or arms.get(default or "")
        if target is None:
            yield Terminate(output=None, reason="no_route")
            return
        out, _ = yield from _run_child(target, state)
        yield Terminate(output=out)
    return program


def race(items: Sequence[ProgramFn]) -> ProgramFn:
    """竞速：首个完成胜出，取消其余（取消走 R1 cancel 设施，算子只编排）。

    Args:
        items: 竞速的 Program 序列。

    Returns:
        新 Program。
    """
    def program() -> Generator[Effect, AgentState, None]:
        gens = [child() for child in items]
        pending: dict[int, Effect] = {}
        first_output: DynamicValue = None
        for i, gen in enumerate(gens):
            try:
                effect = next(gen)
            except StopIteration:
                continue
            if isinstance(effect, Terminate):
                if first_output is None:
                    first_output = effect.output
            else:
                pending[i] = effect
        while pending:
            slots = list(pending.keys())
            batch = [pending[slot] for slot in slots]
            injected_raw: DynamicValue = yield batch  # type: ignore[misc]
            values = (
                list(injected_raw)
                if isinstance(injected_raw, (list, tuple))
                else [injected_raw] * len(slots)
            )
            for slot, value in zip(slots, values):
                pending.pop(slot)
                # 已有胜出者：不再推进其余（取消语义交 R1，算子只做编排）。
                if first_output is not None:
                    continue
                try:
                    effect = gens[slot].send(value)
                except StopIteration:
                    continue
                if isinstance(effect, Terminate):
                    if first_output is None:
                        first_output = effect.output
                else:
                    pending[slot] = effect
            if first_output is not None:
                pending.clear()
        yield Terminate(output=first_output)
    return program


def loop(
    body: ProgramFn,
    *,
    cond: Callable[[AgentState], bool],
    max_rounds: int = 3,
) -> ProgramFn:
    """显式循环：cond 为真时重复 body，轮次上限交 Budgets（此处仅防御性护栏）。

    Args:
        body: 循环体 Program。
        cond: 状态 → 是否继续的纯函数。
        max_rounds: 防御性轮次上限（真实约束由 R1 Budgets.rounds 强制）。

    Returns:
        新 Program。
    """
    def program() -> Generator[Effect, AgentState, None]:
        state = yield Invoke(port="state").with_payload(op="read")
        state = _as_state(state)
        state = state.set(_Keys.LOOP_ROUND, 0)
        while cond(state) and int(state.get(_Keys.LOOP_ROUND, 0)) < max_rounds:
            out, state = yield from _run_child(body, state)
            state = state.set(_Keys.LOOP_ROUND, int(state.get(_Keys.LOOP_ROUND, 0)) + 1)
            state = state.set(_Keys.SEQ_OUTPUT, out)
        yield Terminate(output=state.get(_Keys.SEQ_OUTPUT))
    return program


def map_state(lens: Callable[[AgentState], AgentState]) -> Callable[[ProgramFn], ProgramFn]:
    """State 视图变换算子：返回可作用于一个 Program 的纯变换。

    Args:
        lens: State → State 的纯变换。

    Returns:
        接收 Program、返回新 Program 的高阶函数。
    """
    def apply(child: ProgramFn) -> ProgramFn:
        def program() -> Generator[Effect, AgentState, None]:
            state = yield Invoke(port="state").with_payload(op="read")
            state = _as_state(state)
            state = lens(state)
            out, _ = yield from _run_child(child, state)
            yield Terminate(output=out)
        return program
    return apply


def map_effect(fn: Callable[[Effect], Effect]) -> Callable[[ProgramFn], ProgramFn]:
    """Effect payload 变换：脱敏 / 参数规范化 / 注入标记 / 软预算注入。

    Args:
        fn: Effect → Effect 的纯函数（Terminate 原样透传，避免破坏终态）。

    Returns:
        接收 Program、返回新 Program 的高阶函数。
    """
    def apply(child: ProgramFn) -> ProgramFn:
        def program() -> Generator[Effect, AgentState, None]:
            gen = child()
            try:
                effect = next(gen)
                while True:
                    if isinstance(effect, Terminate):
                        yield effect
                        return
                    sent: AgentState = yield fn(effect)
                    effect = gen.send(sent)
            except StopIteration:
                return
        return program
    return apply


def fallback(
    p: ProgramFn,
    *,
    ladders: Sequence[ProgramFn],
    refuse: ProgramFn,
) -> ProgramFn:
    """降级选级骨架：按序尝试 p → ladders，全失败走 refuse。

    Args:
        p: 首选 Program。
        ladders: 降级阶梯。
        refuse: 兜底拒答 Program。

    Returns:
        新 Program。
    """
    arms = [p, *ladders, refuse]

    def program() -> Generator[Effect, AgentState, None]:
        state = AgentState().set(_Keys.ATTEMPT, 0)
        for arm in arms:
            out, state = yield from _run_child(arm, state)
            if not state.get(_Keys.FAIL_FLAG, False):
                yield Terminate(output=out)
                return
            state = state.set(_Keys.ATTEMPT, int(state.get(_Keys.ATTEMPT, 0)) + 1)
        yield Terminate(output=None, reason="all_failed")
    return program


def gate(p: ProgramFn, policy: Callable[[AgentState], object]) -> ProgramFn:
    """审批/澄清门：可 yield Suspend 挂起，resume 后续跑 p（D-6 唯一能挂起的 A 类算子）。

    Args:
        p: 审批通过后执行的 Program。
        policy: 状态 → 审批策略；返回 None 表示无需审批。

    Returns:
        新 Program。
    """
    def program() -> Generator[Effect, AgentState, None]:
        state = yield Invoke(port="state").with_payload(op="read")
        state = _as_state(state)
        answer = state.last_wait_human
        if policy(state) is not None and answer is None:
            state = yield Suspend(question="approval", options=("yes", "no"))
        out, _ = yield from _run_child(p, state)
        yield Terminate(output=out)
    return program


def retry(p: ProgramFn, *, attempts: int = 3, backoff: float = 0.0) -> ProgramFn:
    """流程级重放：失败按 program_factory 重建重跑（纯函数、状态进 x-*，D-5/D-8）。

    Args:
        p: 被包裹的 Program（每次重跑重新调用，等价重建）。
        attempts: 最大尝试次数。
        backoff: 退避秒数（写入 scratch 交 R3 装饰器，不在算子内 sleep）。

    Returns:
        新 Program。
    """
    def program() -> Generator[Effect, AgentState, None]:
        state = AgentState().set(_Keys.RETRY_COUNT, 0)
        out: DynamicValue = None
        for _ in range(max(1, attempts)):
            out, state = yield from _run_child(p, state)
            if not state.get(_Keys.FAIL_FLAG, False):
                break
            count = int(state.get(_Keys.RETRY_COUNT, 0)) + 1
            state = state.set(_Keys.RETRY_COUNT, count).set(
                _Keys.PREFIX + "backoff", backoff
            )
        yield Terminate(output=out)
    return program

def loop_until(
    step: Callable[[DynamicValue], ProgramFn],
    *,
    done: Callable[[DynamicValue], bool],
    max_rounds: int = 30,
    reason_fn: Callable[[], str] | None = None,
) -> ProgramFn:
    """回注值驱动的循环：每轮把上一步回注值交给 step 生成下一步 Program，done 为真停止。

    与 loop 的区别：loop 读 `port="state"`（拿 AgentState），本算子只读 port 回注的
    Result.data——ReAct 的 tool_calls 决策就在这上面，不需要 state port（真实 Runner
    无该 port，红队 B5）。纯函数：step/done 都是外部注入的纯 callable，本算子不持进度。

    Args:
        step: 上一步回注值 → 下一轮 Program 工厂（纯）。
        done: 回注值 → 是否结束（纯）。
        max_rounds: 防御上限；真实约束仍由 R1 Budgets 兜底。

    Returns:
        新 Program（不改 step/done）。
    """
    def program() -> Generator[Effect, AgentState, None]:
        feedback: DynamicValue = None
        last: DynamicValue = None
        state = AgentState()
        converged = False
        for _ in range(max(1, max_rounds)):
            out, state = yield from _run_child(step(feedback), state)
            last = out
            if done(out):
                converged = True
                break
            feedback = out
        # 终态 reason：架构可注入（handoff/graph 的静态条件），缺省沿用收敛/耗尽语义。
        reason = (reason_fn() if reason_fn is not None
                  else ("" if converged else "turns_exhausted"))
        yield Terminate(output=last, reason=reason)
    return program


def branch_value(
    source: ProgramFn,
    fn: Callable[[DynamicValue], str],
    arms: dict[str, ProgramFn],
    *,
    default: str = "",
    no_route_reason: str = "no_route",
) -> ProgramFn:
    """值驱动分支：从 source 的 Terminate.output 取值选 arm（不经 state port）。

    与 branch 的区别：branch 首 yield Invoke(port="state") 读 AgentState（真实 Runner
    无此 port）；本算子从 source Program 的回注值决策——Router 的 IntentResult 即此类。
    纯函数：fn 外部注入、arms 不改。

    Args:
        source: 产生决策值的 Program 工厂。
        fn: 决策值 → arm key 的纯函数。
        arms: key → Program 工厂。
        default: 无匹配时的兜底 key。
        no_route_reason: 无 arm 时的 Terminate.reason。

    Returns:
        新 Program（不改 source/arms）。
    """
    def program() -> Generator[Effect, AgentState, None]:
        raw, state = yield from _run_child(source, AgentState())
        key = fn(raw)
        target = arms.get(key) or arms.get(default)
        if target is None:
            yield Terminate(output=None, reason=no_route_reason)
            return
        out, _ = yield from _run_child(target, state)
        yield Terminate(output=out)
    return program
