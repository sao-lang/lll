"""Runner：唯一的 async IO 编排点。

链路顺序（M1 §2）：预算预检 → 驱动 generator → batch 展开 → dispatch(M2) →
call(M3) → fold 纯函数回填 → 事件溯源 → checkpoint → 继续 / 挂起 / 结束。

依赖注入：dispatch / call / store / ctx_factory 均可替换；store 未注入时使用内置内存实现，
因此 M1 无需外部服务即可独立闭环。
协作约定：标准集成下 M2 在洋葱中心调用 `ctx.invoke`（即 M3 的 call）；
若 dispatch 只做判决、未产出 data，Runner 兜底直接调用 call（最小 dispatch 场景）。
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, replace

from .errors import (
    AgentKitError,
    PortMissing,
    ProgramCrashed,
    ProgramInvalid,
)
from .fold import fold, replay_events
from .interpreter import EffectBatch, InterpreterImpl, SendResult
from .types import (
    DONE,
    AgentState,
    CallFn,
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
    """M1 内置默认 checkpoint 存储：进程内存字典（M7 提供持久化实现）。"""

    def __init__(self) -> None:
        self.snapshots: dict[str, JsonSnapshot] = {}  # session_id → 快照

    def save(self, session_id: str, snapshot: JsonSnapshot) -> None:
        """把快照按会话 id 存入内存字典。"""
        self.snapshots[session_id] = snapshot

    def load(self, session_id: str) -> JsonSnapshot | None:
        """按会话 id 读取快照；不存在返回 None。"""
        return self.snapshots.get(session_id)


@dataclass
class _Runtime:
    """单个会话在 Runner 内的可变执行态（与 Session 分离，便于重放重建）。"""

    interpreter: InterpreterImpl  # 驱动 program 的解释器
    last_value: DynamicValue = None  # 下一步要 send 回 program 的上次结果
    step: int = 0  # 已执行的 step 计数


def _send_value(event: Event) -> DynamicValue:
    """把事件里的 Result 信封还原成 Program 当初 send 收到的值。

    成功取 data，失败保留整个 Result（让 program 能读到 error）；批量事件返回列表。
    """
    values = [r.data if r.ok else r for r in event.results]
    if event.batched:
        return values
    return values[0] if values else None


async def _maybe_await(value: DynamicValue) -> DynamicValue:
    """统一等待：dispatch / call 既可是同步值也可是 awaitable，这里抹平差异。"""
    return await value if inspect.isawaitable(value) else value


class Runner:
    """唯一的 async IO 编排点；所有可替换依赖走构造注入，M1 可独立闭环。"""

    def __init__(
        self,
        dispatch: DispatchFn,
        call: CallFn,
        *,
        store: EventStore | None = None,
        ctx_factory: Callable[[Effect, Session], DynamicValue] | None = None,
        checkpoint_every: int = 1,
        max_steps: int = 1000,
    ) -> None:
        """装配 Runner：dispatch（M2 判决）与 call（M3 调用）为必注入函数。

        ctx_factory 默认产出 RuntimeCtx；M2 可换成 ChainCtx（字段超集）。
        checkpoint_every 控制快照频率；max_steps 是 run 的硬步数上限。
        """
        self._dispatch = dispatch
        self._call = call
        self._store = store or InMemoryEventStore()
        self._ctx_factory = ctx_factory or (
            lambda effect, session: RuntimeCtx(
                effect=effect, state=session.state, session_id=session.session_id
            )
        )
        self._checkpoint_every = max(1, checkpoint_every)
        self._max_steps = max_steps
        self._runtimes: dict[str, _Runtime] = {}  # session_id → 执行态
        self._reasons: dict[str, str] = {}  # session_id → 最近一次状态迁移原因
        self._errors: dict[str, str | None] = {}  # session_id → 最近一次错误码

    # ------------------------------------------------------------------ #
    # 内部：运行时状态 / 重放 / 单 Effect 执行
    # ------------------------------------------------------------------ #
    def _runtime(self, session: Session) -> _Runtime:
        """取（或懒建）会话执行态；首次创建时用工厂重建 generator 并回放事件到断点。"""
        rt = self._runtimes.get(session.session_id)
        if rt is None:
            rt = _Runtime(InterpreterImpl(session.program_factory()))
            self._runtimes[session.session_id] = rt
            self._replay(session, rt)
        return rt

    def _replay(self, session: Session, rt: _Runtime, answer: DynamicValue = None) -> None:
        """从事件日志把新建 generator 推进到断点（Program 纯函数 ⇒ 轨迹必然一致）。

        事件 i 记录「产出 effects_i + 结果 v_i」；要让 generator 停在最后一个 yield 处，
        需依次 send(None), v_0, ..., v_{n-2}，再把 v_{n-1}（或人工答案）留给下一次 step。
        """
        events = session.events
        if not events:
            return
        if rt.interpreter.send(None) is DONE:
            return
        for event in events[:-1]:
            if event.status in ("done", "error"):
                return
            if rt.interpreter.send(_send_value(event)) is DONE:
                return
        last = events[-1]
        if last.status == "suspended":
            rt.last_value = answer if answer is not None else _send_value(last)
        elif last.status == "running":
            rt.last_value = _send_value(last)
        else:
            rt.last_value = None

    @staticmethod
    def _bind(ctx: DynamicValue, runner: Runner) -> DynamicValue:
        """给上下文注入 invoke 回调，让链路的洋葱中心能调用 M3 的 call（M2 不 import M3）。"""
        ctx.invoke = lambda effect, c: runner._call(effect, c)
        return ctx

    async def _execute(
        self, effect: Effect, ctx: DynamicValue, session: Session
    ) -> tuple[Result, tuple[Event, ...]]:
        """执行单个 Effect，返回 Result 与子会话事件。

        判决优先级：dispatch 返回 None → Runner 自调 call；返回 Result → 直接用；
        返回带 abort/verdict/data 的判决对象 → 分别折叠；任何异常 → 错误信封。
        """
        if effect.kind is EffectKind.CHILD:
            return await self._child(effect, session)
        try:
            outcome = await _maybe_await(self._dispatch(effect, ctx))
            if outcome is None:  # 最小 dispatch：未产出 data 时 Runner 自己调 call
                return await _maybe_await(self._call(effect, ctx)), ()
            if isinstance(outcome, Result):
                return outcome, ()
            if getattr(outcome, "abort", False):
                reason = getattr(getattr(outcome, "verdict", None), "reason", "") or "chain aborted"
                return Result.failure("CHAIN_ABORTED", reason), ()
            data = getattr(outcome, "data", None)
            if isinstance(data, Result):
                return data, ()
            if data is not None:
                usage = getattr(data, "usage", None)
                return Result.success(data, usage=usage), ()
            return await _maybe_await(self._call(effect, ctx)), ()
        except AgentKitError as exc:
            return Result.failure(exc.code, str(exc)), ()
        except Exception as exc:  # 异常绝不抛穿运行时，交 M2 回程 filter / StepReport  # noqa: BLE001
            return Result.failure("PORT_CRASHED", f"{type(exc).__name__}: {exc}"), ()

    async def _child(self, effect: Effect, session: Session) -> tuple[Result, tuple[Event, ...]]:
        """Effect(child) 递归创建子 Session，复用同一解释器与注入函数。"""
        payload = effect.payload
        factory = payload.get("program_factory") # pyright: ignore[reportAssignmentType]
        if factory is None:
            program = payload.get("program")
            if program is None:
                return Result.failure("CHILD_PROGRAM_MISSING", "child needs program_factory"), ()
            def factory(p: Program = program) -> Program:
                return p
        child_state: AgentState = payload.get("state") or session.state
        child_state = child_state.with_(budgets=child_state.budgets.consume(hops=1))
        child = Session(
            session_id=f"{session.session_id}:child:{len(session.events)}",
            program_factory=factory,
            state=child_state,
        )
        res = await self.run(child)
        return Result.success(res.output, usage=Usage()), tuple(res.events)

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    async def step(self, session: Session) -> StepReport:
        """执行一个 step（供调试 / HITL 单步使用）。"""
        try:
            rt = self._runtime(session)
        except (ProgramInvalid, ProgramCrashed) as exc:
            return StepReport(
                step=0, state=session.state, status="error",
                error=exc.code, reason=str(exc), events=tuple(session.events),
            )
        step_no = rt.step
        rt.step += 1
        state = session.state
        effects: tuple[Effect, ...] = ()
        results: tuple[Result, ...] = ()
        batch: SendResult | None = None
        status = "running"
        reason = ""
        error: str | None = None

        # 0. 预算预检：超限强制 END
        exceeded = state.budgets.exceeded()
        if exceeded:
            reason = f"BUDGET_EXCEEDED:{exceeded}"
            effects = (Effect(EffectKind.END, {"reason": reason}),)
            results = (Result.success(None),)
            status = "done"
        else:
            try:
                batch = rt.interpreter.send(rt.last_value)
            except (ProgramInvalid, ProgramCrashed) as exc:
                status, error, reason = "error", exc.code, str(exc)
                batch = EffectBatch(())
            except Exception as exc:  # pragma: no cover - 防御：任何 program 崩溃都进报告  # noqa: BLE001
                status, error, reason = "error", "PROGRAM_CRASHED", str(exc)
                batch = EffectBatch(())

            # 1. program 自然返回 → 合成一个 END Effect，记为 done
            if status == "running" and batch is DONE:
                status, reason = "done", "program_returned"
                effects = (Effect(EffectKind.END, {"reason": reason}),)
                results = (Result.success(None),)
            elif status == "running":
                assert isinstance(batch, EffectBatch)
                # 2. 为每个 Effect 注入确定性 trace_id（session:step:index）
                effects = tuple(
                    replace(e, trace_id=f"{session.session_id}:{step_no:04d}:{i:02d}")
                    for i, e in enumerate(batch.effects)
                )
                # 3. 并行数超限强制 END；END / WAIT_HUMAN 特殊终端不走 port
                if state.budgets.check_parallel(len(effects)):
                    reason = "BUDGET_EXCEEDED:parallel_children"
                    effects = (Effect(EffectKind.END, {"reason": reason}),)
                    results = (Result.success(None),)
                    status = "done"
                elif effects and effects[0].kind is EffectKind.END:
                    results = (Result.success(None),)
                    status, reason = "done", str(effects[0].payload.get("reason", "end"))
                elif effects and effects[0].kind is EffectKind.WAIT_HUMAN:
                    results = (Result.success(None),)
                    status, reason = "suspended", "wait_human"
                else:
                    # 4. 普通 Effect：并行构造上下文并并发执行（IO 的唯一汇聚点）
                    ctxs = [self._bind(self._ctx_factory(e, session), self) for e in effects]
                    executed = await asyncio.gather(
                        *(self._execute(e, c, session) for e, c in zip(effects, ctxs))
                    )
                    results = tuple(item[0] for item in executed)
                    # 子会话事件先入日志，保证 seq 顺序在父事件之前
                    child_events = tuple(ev for item in executed for ev in item[1])
                    for ev in child_events:
                        session.events.append(ev)
                    # 任一结果 abort → 整个 run 置 error
                    if any(not r.ok and r.error and r.error.code == "CHAIN_ABORTED" for r in results):
                        status, reason = "error", "chain aborted"
                        error = next(
                            r.error.message for r in results
                            if not r.ok and r.error and r.error.code == "CHAIN_ABORTED"
                        )
                    rt.last_value = [r.data if r.ok else r for r in results] if batch.batched else (
                        results[0].data if results and results[0].ok else (results[0] if results else None)
                    )

        # 5. 纯函数 fold 回填 State；随后累计 usage 写进事件元数据
        new_state = fold(state, effects, results, reason)
        session.state = new_state
        cost = sum(float(getattr(r.usage, "cost_usd", 0.0) or 0.0) for r in results if r.usage)
        tokens = sum(
            (getattr(r.usage, "prompt_tokens", 0) or 0) + (getattr(r.usage, "completion_tokens", 0) or 0)
            for r in results if r.usage
        )
        event = Event(
            seq=len(session.events),
            session_id=session.session_id,
            step=step_no,
            state_snapshot=new_state,
            effects=effects,
            results=results,
            batched=bool(getattr(batch, "batched", False)),
            ts=step_no,
            cost_usd=cost,
            tokens=tokens,
            status=status,
            reason=reason,
        )
        session.events.append(event)
        self._reasons[session.session_id] = reason
        self._errors[session.session_id] = error
        # 6. 终态或达到频率就落一次 checkpoint
        if status != "running" or rt.step % self._checkpoint_every == 0:
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
        """跑到 done / suspended / error。"""
        if session.events and session.events[-1].status in ("done", "error", "suspended"):
            return self._result(session)
        return await self._loop(session)

    async def resume(self, session: Session, answer: DynamicValue) -> RunResult:
        """从挂起断点继续：答案 send 回 Program，事件日志不动（跨周期恢复入口）。"""
        rt = _Runtime(InterpreterImpl(session.program_factory()))
        self._runtimes[session.session_id] = rt
        self._replay(session, rt, answer=answer)
        return await self._loop(session, initial_value=answer)

    async def _loop(self, session: Session, initial_value: DynamicValue = None) -> RunResult:
        """反复 step 直到进入终态或触碰 max_steps 硬上限，再汇总为 RunResult。"""
        rt = self._runtime(session)
        if initial_value is not None:
            rt.last_value = initial_value
        for _ in range(self._max_steps):
            report = await self.step(session)
            if report.status != "running":
                break
        return self._result(session)

    def _result(self, session: Session) -> RunResult:
        """从会话终态与缓存的 reason/error 汇总 RunResult；output 取 scratchpad。"""
        last = session.events[-1] if session.events else None
        status = last.status if last else "running"
        return RunResult(
            session_id=session.session_id,
            state=session.state,
            status=status,
            events=tuple(session.events),
            output=session.state.output,  # 直接读显式字段（不再经字符串代理）
            reason=self._reasons.get(session.session_id, ""),
            error=self._errors.get(session.session_id),
        )

    def replay(self, session: Session, initial_state: AgentState) -> AgentState:
        """事件溯源重放：从初始 State 逐步 fold 得到终态（M1-T03）。"""
        return replay_events(session.events, initial_state)

    def load(self, session_id: str, program_factory: Callable[[], Program]) -> Session:
        """从 checkpoint 恢复 Session（新进程 / 跨周期断点恢复）。"""
        snap = self._store.load(session_id)
        if snap is None:
            raise PortMissing(f"no checkpoint for session {session_id}")
        return restore_checkpoint(snap, program_factory)
