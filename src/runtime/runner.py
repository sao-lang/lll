"""R1 Runner：唯一的 async IO 编排点。

链路顺序：预算预检 → 驱动 generator → batch 展开 → dispatch(上层) →
call(Port) → fold 纯函数回填 → 事件溯源 → checkpoint → 继续/挂起/结束。

依赖注入：dispatch / call / store / ctx_factory 均可替换；store 未注入时用内存实现，
因此 R1 无需外部服务即可独立闭环。所有异常在此折叠为 Result，绝不裸抛穿运行时。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import cast

from .errors import AgentKitError, ChainAborted, PortMissing, ProgramCrashed, ProgramInvalid
from .fold import field, fold, replay_events
from .interpreter import EffectBatch, InterpreterImpl, SendResult
from .types import (
    DONE,
    AgentState,
    CallFn,
    CancellationToken,
    DispatchFn,
    DynamicValue,
    Effect,
    EffectKind,
    Event,
    EventStore,
    JsonSnapshot,
    Program,
    Result,
    RunResult,
    RuntimeCtx,
    Session,
    StepReport,
    Usage,
    checkpoint,
    restore_checkpoint,
)

__all__ = ["InMemoryEventStore", "Runner"]


class InMemoryEventStore(EventStore):
    """R1 内置默认 checkpoint 存储：进程内存字典。"""

    def __init__(self) -> None:
        """初始化内存快照表（无持久化，仅用于独立闭环测试）。"""
        self.snapshots: dict[str, JsonSnapshot] = {}

    def save(self, session_id: str, snapshot: JsonSnapshot) -> None:
        """把快照按会话 id 存入内存字典。

        参数：
            session_id: 会话标识（字典键）。
            snapshot: 可 JSON 持久化的快照信封。
        """
        self.snapshots[session_id] = snapshot

    def load(self, session_id: str) -> JsonSnapshot | None:
        """按会话 id 读取快照；不存在返回 None。"""
        return self.snapshots.get(session_id)


@dataclass
class _Runtime:
    """单个会话在 Runner 内的可变执行态（与 Session 分离，便于重放重建）。

    字段：
        interpreter: 该会话的 generator 解释器，承载推进游标。
        last_value: 上一次 Effect 的结果，下一步 send 回 Program。
        step: 当前步号（逻辑时钟，Event.ts 与 trace_id 都取自它）。
    """

    interpreter: InterpreterImpl
    last_value: DynamicValue = None
    step: int = 0


# --------------------------------------------------------------------------- #
# 裁决契约（私有）：R1 不 import R2，用结构化投影替代散落的 getattr + 魔法串。
# 全部下划线开头 —— G5 契约锁只提取非下划线公共类，故不触发契约基线变更。
# --------------------------------------------------------------------------- #

class _VerdictAction(str, Enum):
    """裁决动作枚举：值与 R2 DecisionAction 对齐，R1 侧独立定义以免反向依赖。"""
    CONTINUE = "continue"
    ABORT = "abort"
    DENY = "deny"
    FALLBACK = "fallback"


class _ErrCode(str, Enum):
    """Runner 内部折叠错误码：值与 errors.py 的稳定 code 对齐，不新增公共类。"""
    CHAIN_ABORTED = "CHAIN_ABORTED"
    PORT_CRASHED = "PORT_CRASHED"
    PROGRAM_CRASHED = "PROGRAM_CRASHED"


class _StepStatus(str, Enum):
    """单步状态枚举：值与 Event / StepReport.status 的既有字符串一致。"""
    RUNNING = "running"
    DONE = "done"
    SUSPENDED = "suspended"
    ERROR = "error"


_VERDICT_BY_NAME = {member.value: member for member in _VerdictAction}
"""动作字符串 → 枚举查表；未知名回退 CONTINUE，保持原 fall-through 语义。"""


@dataclass(frozen=True, slots=True)
class _Verdict:
    """判决对象的规范化投影：把鸭子判决字段一次性读成具名、带类型的视图。

    字段：
        action: 裁决动作（枚举，未知动作归一为 CONTINUE）。
        reason: 拒绝 / 终止原因。
        value: 裁决载体（fallback 兜底值 / around 回填的 Result / 普通值）。
    """
    action: _VerdictAction = _VerdictAction.CONTINUE
    reason: str = ""
    value: DynamicValue = None


type _DispatchOutcome = Result | _Verdict | None
"""dispatch 的规范化返回集：None 放行 / Result 采纳 / _Verdict 待裁决。"""


def _coerce_verdict(outcome: DynamicValue) -> _Verdict:
    """把 dispatch 的鸭子判决归一为 _Verdict；唯一的动态字段读取边界。

    getattr 集中于此，下游只访问具名类型字段（编码规则 4）。
    历史兼容：abort 为 callable（Decision.abort 类方法）不算标志，布尔真值才算。
    """
    raw_action: DynamicValue = getattr(outcome, "action", None)
    action_name = str(getattr(raw_action, "value", raw_action))
    action = _VERDICT_BY_NAME.get(action_name, _VerdictAction.CONTINUE)
    reason = str(getattr(outcome, "reason", "") or "")
    value: DynamicValue = getattr(outcome, "value", None)
    abort_flag: DynamicValue = getattr(outcome, "abort", False)
    if not callable(abort_flag) and abort_flag:
        action = _VerdictAction.ABORT
        nested = getattr(outcome, "verdict", None)
        reason = str(getattr(nested, "reason", "") or "") or "chain aborted"
    return _Verdict(action=action, reason=reason, value=value)


def _settle(verdict: _Verdict) -> Result | None:
    """把规范化判决折叠为 Result；返回 None 表示放行、需真正调用 Port。

    参数：
        verdict: 已归一的结构化判决（由 _coerce_verdict 产出）。
    返回：
        裁决 Result，或 None（continue 且无裁决载体 → 放行）。
    """
    if verdict.action in (_VerdictAction.ABORT, _VerdictAction.DENY):
        # 终止 / 拒绝：不调用 Port，reason 由插件提供。
        return Result.failure(
            _ErrCode.CHAIN_ABORTED.value, verdict.reason or "chain aborted"
        )
    if verdict.action is _VerdictAction.FALLBACK:
        # 兜底：用插件提供的值作为结果，不调用 Port。
        return Result.success(verdict.value)
    # around 中心 invoke 的 Result 装在 value 里，直接采纳，保证副作用只执行一次。
    if isinstance(verdict.value, Result):
        return verdict.value
    if verdict.value is not None:
        usage = cast(Usage | None, getattr(verdict.value, "usage", None))
        return Result.success(verdict.value, usage=usage)
    return None


def _send_value(event: Event) -> DynamicValue:
    """把事件里的 Result 还原成 Program 当初 send 收到的值。

    分支：
        - batched：返回 list（每个 Result 取 data，失败则取 Result 本身）。
        - 单个：取首个值的 data / Result；无结果返回 None。
    注意失败时用 Result 对象本身（而非 data=None）回填，让 Program 能读到错误信封。
    """
    values = [r.data if r.ok else r for r in event.results]
    if event.batched:
        return values
    return values[0] if values else None


async def _maybe_await(value: DynamicValue) -> DynamicValue:
    """统一等待：dispatch/call 既可是同步值也可是 awaitable。

    参数：
        value: 同步返回值或 Awaitable。
    返回：
        解包后的实际值。
    """
    return await value if inspect.isawaitable(value) else value


class Runner:
    """唯一 async IO 编排点；所有可替换依赖走构造注入，R1 可独立闭环。"""

    def __init__(
        self,
        dispatch: DispatchFn,
        call: CallFn,
        *,
        store: EventStore | None = None,
        ctx_factory: Callable[[Effect, Session], DynamicValue] | None = None,
        checkpoint_every: int = 1,
        max_steps: int = 1000,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """装配 Runner：dispatch（上层判决）与 call（Port 调用）为必注入函数。

        参数：
            dispatch: 链路判决函数（R2 HookBus.emit），返回 Decision 或 None。
            call: Port 调用函数；dispatch 放行时真正执行外部副作用。
            store: checkpoint 存储；缺省用 InMemoryEventStore。
            ctx_factory: 上下文工厂；缺省构造 RuntimeCtx。上层可换成 HookCtx。
            checkpoint_every: 每 N 步落一次快照（终态总是落），最小强制为 1。
            max_steps: 单 run 的步数硬上限，防止死循环。
            cancellation: 取消令牌；缺省不取消。子 Agent 派生时继承同一令牌（A5 递归传播）。
        """
        self._dispatch = dispatch  # 上层链路判决
        self._call = call  # Port 实际调用
        self._store = store or InMemoryEventStore()  # 快照存储（缺省内存）
        self._ctx_factory = ctx_factory or (  # 上下文工厂（缺省 RuntimeCtx）
            lambda effect, session: RuntimeCtx(
                effect=effect, state=session.state, session_id=session.session_id
            )
        )
        self._checkpoint_every = max(1, checkpoint_every)  # 至少每步落盘
        self._max_steps = max_steps  # 步数硬上限
        self._cancellation = cancellation  # 取消令牌（A5；None 表示不可取消）
        self._runtimes: dict[str, _Runtime] = {}  # session_id → 执行态
        self._reasons: dict[str, str] = {}  # session_id → 最近一次结束原因
        self._errors: dict[str, str | None] = {}  # session_id → 最近一次错误码
        # 子会话序号按父 session 独立递增：同批并发 CHILD 必须拿到唯一 id，
        # 否则 _runtimes/_reasons/_errors 会互相覆盖（多 Agent 并行扇出的坑）。
        # asyncio 单线程中 "读 seq → 写 seq+1" 之间没有 await，故并发安全。
        self._child_seq: dict[str, int] = {}

    def _runtime(self, session: Session) -> _Runtime:
        """取（或懒建）会话执行态；首次创建时重建 generator 并回放事件到断点。

        参数：
            session: 目标会话。
        返回：
            该会话的执行态（已推进到事件日志末尾对应位置）。
        """
        rt = self._runtimes.get(session.session_id)
        if rt is None:  # 首次：新建解释器并回放已有事件
            rt = _Runtime(InterpreterImpl(session.program_factory()))
            self._runtimes[session.session_id] = rt
            self._replay(session, rt)
        return rt

    def _replay(
        self, session: Session, rt: _Runtime, answer: DynamicValue = None
    ) -> None:
        """从事件日志把新建 generator 推进到断点（Program 纯函数 ⇒ 轨迹一致）。

        参数：
            session: 含事件日志的会话。
            rt: 待推进的执行态。
            answer: 恢复时的人工答案；仅当末事件为 suspended 时作为最后回填值。
        """
        events = session.events
        if not events:
            return  # 无事件：generator 停在初始态
        if rt.interpreter.send(None) is DONE:
            return  # 首次 next() 即结束：空 Program
        # 回放除末事件外的全部事件；遇到 done/error 说明已到终态，停止。
        for event in events[:-1]:
            if event.status in (_StepStatus.DONE.value, _StepStatus.ERROR.value):
                return
            if rt.interpreter.send(_send_value(event)) is DONE:
                return
        # 末事件决定最后回填给 generator 的值：
        last = events[-1]
        if last.status == _StepStatus.SUSPENDED.value:
            # 挂起恢复：优先用人工 answer，否则沿用事件里的结果。
            rt.last_value = answer if answer is not None else _send_value(last)
        elif last.status == _StepStatus.RUNNING.value:
            rt.last_value = _send_value(last)  # 正常运行：回填事件结果
        else:
            rt.last_value = None  # done/error：无需再回填

    @staticmethod
    def _bind(ctx: DynamicValue, runner: Runner) -> DynamicValue:
        """给上下文注入 invoke 回调，让链路洋葱中心能调用 Port call。

        参数：
            ctx: 上下文对象（RuntimeCtx 或 HookCtx），其 invoke 字段被覆写。
            runner: 提供 call 的 Runner 实例（用闭包绑定）。
        返回：
            注入 invoke 后的同一上下文对象（原地设置字段）。
        """
        ctx.invoke = lambda effect, c: runner._call(effect, c)
        # A5：子 Agent 继承父 Runner 的取消令牌（递归传播）。
        ctx.cancellation = runner._cancellation
        return ctx

    async def _execute(
        self, effect: Effect, ctx: DynamicValue, session: Session
    ) -> tuple[Result, tuple[Event, ...]]:
        """执行单个 Effect，返回 Result 与子会话事件。

        参数：
            effect: 待执行 Effect。
            ctx: 已注入 invoke 的上下文，传给 dispatch/call。
            session: 当前会话（CHILD fork 时作为父会话）。
        返回：
            (Result, 子会话产生的事件元组)。非 CHILD 时事件元组为空。
        """
        if effect.kind is EffectKind.CHILD:
            return await self._child(effect, session)  # 子 Agent 走递归分支
        try:
            outcome = await _maybe_await(self._dispatch(effect, ctx))
            if outcome is None:
                # 判决放行（None）：直接调用 Port。
                return await _maybe_await(self._call(effect, ctx)), ()
            if isinstance(outcome, Result):
                # 判决直接给出 Result：采纳，不再调用 Port。
                return outcome, ()
            # 结构化判决归一为具名视图，再折叠；内核不 import R2 的 Decision。
            settled = _settle(_coerce_verdict(outcome))
            if settled is not None:
                return settled, ()
            # continue 且无裁决载体：判决放行，真正调用 Port。
            return await _maybe_await(self._call(effect, ctx)), ()
        except AgentKitError as exc:
            # 框架异常：用自身 code 折叠为失败信封。
            return Result.failure(exc.code, str(exc)), ()
        except Exception as exc:  # 异常绝不抛穿运行时  # noqa: BLE001
            # 未预期异常统一折叠，保留异常类型名便于诊断。
            return Result.failure(
                _ErrCode.PORT_CRASHED.value, f"{type(exc).__name__}: {exc}"
            ), ()

    async def _child(
        self, effect: Effect, session: Session
    ) -> tuple[Result, tuple[Event, ...]]:
        """Effect(child) 递归创建子 Session，复用同一解释器与注入函数。

        参数：
            effect: CHILD Effect，payload 携带 program_factory/program/state。
            session: 父会话（用于继承 state 与生成子会话 id）。
        返回：
            (以子 run 的 output 为 data 的 Result, 子会话全部事件)。
        """
        payload = effect.payload
        # 优先用可序列化的 program_factory；回退到直接 program（包装成无参工厂）。
        factory: Callable[[], Program] | None = payload.get("program_factory")
        if factory is None:
            child_program: Program | None = payload.get("program")
            if child_program is None:
                # 两种都没给：无法构造子 Program，返回失败信封。
                return Result.failure(
                    "CHILD_PROGRAM_MISSING", "child needs program_factory"
                ), ()
            factory = lambda p=child_program: p  # 默认参数绑定，避免闭包晚绑定
        child_state: AgentState = payload.get("state") or session.state
        child_state = child_state.with_(budgets=child_state.budgets.consume(hops=1))  # 子 Agent 深度 +1
        # 用单调递增序号作子会话 id 后缀，保证同批并发 CHILD 的 id 唯一。
        seq = self._child_seq.get(session.session_id, 0)
        self._child_seq[session.session_id] = seq + 1
        child = Session(
            session_id=f"{session.session_id}:child:{seq}",
            program_factory=factory,
            state=child_state,
        )
        res = await self.run(child)  # 递归跑完整子会话（含其自身的 CHILD）
        return Result.success(res.output, usage=Usage()), tuple(res.events)

    async def step(self, session: Session) -> StepReport:
        """执行一个 step（供调试 / HITL 单步使用）。

        参数：
            session: 目标会话，state/events 会被就地推进（执行态在 Runner 内）。
        返回：
            StepReport：本步步号、新 State、Effect/Result、状态与事件。
        """
        # A5 取消检查：令牌已置位则本步不执行任何 Effect，写 cancelled 事件并报告。
        if self._cancellation is not None and self._cancellation.cancelled:
            session.events.append(Event(
                seq=len(session.events) + 1,
                session_id=session.session_id,
                step=0,
                state_snapshot=session.state,
                effects=(),
                results=(),
                status="cancelled",
                reason=self._cancellation.reason,
            ))
            return StepReport(
                step=0,
                state=session.state,
                status="cancelled",
                reason=self._cancellation.reason,
                events=tuple(session.events),
            )

        try:
            rt = self._runtime(session)
        except (ProgramInvalid, ProgramCrashed) as exc:
            # Program 构造即失败：直接产出 error 报告，不进入本步主流程。
            return StepReport(
                step=0,
                state=session.state,
                status=_StepStatus.ERROR.value,
                error=exc.code,
                reason=str(exc),
                events=tuple(session.events),
            )
        step_no = rt.step  # 本步逻辑时钟
        rt.step += 1  # 游标前进（即使本步失败也推进，保证 trace_id 单调）
        state = session.state
        effects: tuple[Effect, ...] = ()  # 本步最终执行的 Effect
        results: tuple[Result, ...] = ()  # 对应 Result
        batch: SendResult | None = None  # 解释器推进结果（用于 batched 标志）
        status = _StepStatus.RUNNING.value  # 本步状态：running/done/suspended/error
        reason = ""  # 结束/挂起原因
        error: str | None = None  # 错误码（仅 error 态非空）

        # 0. 预算预检：超限强制 END（内核硬强制，Program 无法绕过）
        exceeded = state.budgets.exceeded()
        if exceeded:
            reason = f"BUDGET_EXCEEDED:{exceeded}"
            effects = (Effect(EffectKind.END, {"reason": reason}),)
            results = (Result.success(None),)
            status = _StepStatus.DONE.value
        else:
            # 1. 推进解释器，拿到下一批 Effect 或 DONE。
            try:
                batch = rt.interpreter.send(rt.last_value)
            except (ProgramInvalid, ProgramCrashed) as exc:
                status, error, reason = _StepStatus.ERROR.value, exc.code, str(exc)
                batch = EffectBatch(())  # 空批次，保证后续字段有值
            except Exception as exc:  # pragma: no cover  # noqa: BLE001
                status, error, reason = (
                    _StepStatus.ERROR.value, _ErrCode.PROGRAM_CRASHED.value, str(exc)
                )
                batch = EffectBatch(())

            if status == _StepStatus.RUNNING.value and batch is DONE:
                # Program 已 return：合成一个 END Effect 收尾。
                status, reason = _StepStatus.DONE.value, "program_returned"
                effects = (Effect(EffectKind.END, {"reason": reason}),)
                results = (Result.success(None),)
            elif status == _StepStatus.RUNNING.value:
                assert isinstance(batch, EffectBatch)
                # 给每个 Effect 打 trace_id：<session>:<step>:<index>，便于事件溯源定位。
                effects = tuple(
                    replace(e, trace_id=f"{session.session_id}:{step_no:04d}:{i:02d}")
                    for i, e in enumerate(batch.effects)
                )
                if state.budgets.check_parallel(len(effects)):
                    # 并行度超限：内核拦截，强制 END。
                    reason = "BUDGET_EXCEEDED:parallel_children"
                    effects = (Effect(EffectKind.END, {"reason": reason}),)
                    results = (Result.success(None),)
                    status = _StepStatus.DONE.value
                elif effects and effects[0].kind is EffectKind.END:
                    # Program 主动 END：不调用 Port，直接置 done。
                    results = (Result.success(None),)
                    status, reason = (
                        _StepStatus.DONE.value,
                        str(effects[0].payload.get("reason", "end")),
                    )
                elif effects and effects[0].kind is EffectKind.WAIT_HUMAN:
                    # 人工在环：本步挂起，等待 resume(answer)。
                    results = (Result.success(None),)
                    status, reason = _StepStatus.SUSPENDED.value, "wait_human"
                else:
                    # 常规批：为每个 Effect 构造上下文并绑定 invoke，然后并发执行。
                    ctxs = [
                        self._bind(self._ctx_factory(e, session), self) for e in effects
                    ]
                    executed = await asyncio.gather(
                        *(self._execute(e, c, session) for e, c in zip(effects, ctxs))
                    )
                    results = tuple(item[0] for item in executed)
                    # 子会话事件挂到父会话日志（子 Agent 的轨迹对父可见）。
                    child_events = tuple(ev for item in executed for ev in item[1])
                    for ev in child_events:
                        session.events.append(ev)
                    # 任一结果被判 abort：本步转 error，取首个 abort 的 reason。
                    if any(
                        not r.ok and r.error and r.error.code == _ErrCode.CHAIN_ABORTED.value
                        for r in results
                    ):
                        status, reason = _StepStatus.ERROR.value, "chain aborted"
                        error = next(
                            r.error.message
                            for r in results
                            if not r.ok and r.error and r.error.code == _ErrCode.CHAIN_ABORTED.value
                        )
                    # 回填给 Program 的值：batch 用 list，单个取 data（失败取 Result 本身）。
                    rt.last_value = (
                        [r.data if r.ok else r for r in results]
                        if batch.batched
                        else (
                            results[0].data
                            if results and results[0].ok
                            else (results[0] if results else None)
                        )
                    )

        # 2. 纯函数折叠：本步的 Effect/Result → 新 State。
        new_state = fold(state, effects, results, reason)
        session.state = new_state
        # 3. 汇总本步 usage（成本/token），写入事件供可观测使用。
        # usage 由外部 port 提供，形态可能是 dataclass / pydantic / dict，用 field 统一读取。
        cost = sum(float(field(r.usage, "cost_usd", 0.0) or 0.0) for r in results if r.usage)
        tokens = sum(
            (field(r.usage, "prompt_tokens", 0) or 0)
            + (field(r.usage, "completion_tokens", 0) or 0)
            for r in results
            if r.usage
        )
        event = Event(
            seq=len(session.events),  # 逻辑序号 = 当前事件数（append-only）
            session_id=session.session_id,
            step=step_no,
            state_snapshot=new_state,
            effects=effects,
            results=results,
            batched=bool(batch.batched) if isinstance(batch, EffectBatch) else False,
            ts=step_no,  # 逻辑时钟（无墙钟，保证确定性）
            cost_usd=cost,
            tokens=tokens,
            status=status,
            reason=reason,
        )
        session.events.append(event)  # 追加事件（含前面已追加的子会话事件）
        self._reasons[session.session_id] = reason
        self._errors[session.session_id] = error
        # 终态总落快照；运行中按 checkpoint_every 节流。
        if status != _StepStatus.RUNNING.value or rt.step % self._checkpoint_every == 0:
            self._store.save(session.session_id, checkpoint(session))
        return StepReport(
            step=step_no,
            state=new_state,
            effects=effects,
            results=results,
            status=status,
            reason=reason,
            error=error,
            events=tuple(session.events),
        )

    async def run(self, session: Session) -> RunResult:
        """跑到 done / suspended / error。

        参数：
            session: 目标会话。
        返回：
            RunResult：终态汇总。已是终态的会话直接返回，不重复执行。
        """
        if session.events and session.events[-1].status in (
            _StepStatus.DONE.value,
            _StepStatus.ERROR.value,
            _StepStatus.SUSPENDED.value,
        ):
            return self._result(session)  # 终态幂等：不重复跑
        return await self._loop(session)

    async def resume(self, session: Session, answer: DynamicValue) -> RunResult:
        """从挂起断点继续：答案 send 回 Program，事件日志不动。

        参数：
            session: 处于 suspended 态的会话。
            answer: 人工答案，作为断点处 Effect 的结果回填。
        返回：
            继续跑到终态的 RunResult。
        """
        # 重建解释器并按事件回放，把 answer 作为末事件（suspended）的回填值。
        rt = _Runtime(InterpreterImpl(session.program_factory()))
        self._runtimes[session.session_id] = rt
        self._replay(session, rt, answer=answer)
        return await self._loop(session, initial_value=answer)

    async def _loop(
        self, session: Session, initial_value: DynamicValue = None
    ) -> RunResult:
        """反复 step 直到终态或触碰 max_steps 硬上限。

        参数：
            session: 目标会话。
            initial_value: 恢复场景的首步回填值（None 表示用执行态已有的 last_value）。
        返回：
            终态 RunResult。
        """
        rt = self._runtime(session)
        if initial_value is not None:
            rt.last_value = initial_value
        for _ in range(self._max_steps):  # 硬上限防死循环
            report = await self.step(session)
            if report.status != _StepStatus.RUNNING.value:
                break  # 到达 done/suspended/error
        return self._result(session)

    def _result(self, session: Session) -> RunResult:
        """从会话终态汇总 RunResult。

        参数：
            session: 目标会话。
        返回：
            RunResult：状态取自末事件，原因/错误取自 Runner 记录。
        """
        last = session.events[-1] if session.events else None
        status = last.status if last else _StepStatus.RUNNING.value  # 无事件视为仍在运行
        return RunResult(
            session_id=session.session_id,
            state=session.state,
            status=status,
            events=tuple(session.events),
            output=session.state.output,
            reason=self._reasons.get(session.session_id, ""),
            error=self._errors.get(session.session_id),
        )

    def replay(self, session: Session, initial_state: AgentState) -> AgentState:
        """事件溯源重放：从初始 State 逐步 fold 得到终态。

        参数：
            session: 含事件日志的会话。
            initial_state: 起始状态。
        返回：
            重放后的终态 State。
        """
        return replay_events(session.events, initial_state)

    def load(self, session_id: str, program_factory: Callable[[], Program]) -> Session:
        """从 checkpoint 恢复 Session。

        参数：
            session_id: 会话标识。
            program_factory: 重建 Program 的无参工厂（checkpoint 不含 generator）。
        返回：
            恢复的 Session（State 与事件日志来自快照）。
        异常：
            PortMissing: 该会话无 checkpoint。
        """
        snap = self._store.load(session_id)
        if snap is None:
            raise PortMissing(f"no checkpoint for session {session_id}")
        return restore_checkpoint(snap, program_factory)
