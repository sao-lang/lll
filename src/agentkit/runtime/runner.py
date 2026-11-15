"""Runner：唯一的 async IO 编排点。

链路顺序（M1 §2）：预算预检 → 驱动 generator → batch 展开 → dispatch(M2) →
call(M3) → fold 纯函数回填 → 事件溯源 → checkpoint → 继续 / 挂起 / 结束。

依赖注入：dispatch / call / store / ctx_factory 全部可替换为 fake，因此 M1 能独立闭环。
协作约定：标准集成下 M2 在洋葱中心调用 `ctx.invoke`（即 M3 的 call）；
若 dispatch 只做判决、未产出 data，Runner 兜底直接调用 call（fake dispatch 场景）。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .errors import AgentKitError, BudgetExceeded, PortMissing, ProgramCrashed, ProgramInvalid
from .fold import fold, replay_events
from .interpreter import EffectBatch, InterpreterImpl
from .types import (
    DONE,
    AgentState,
    CallFn,
    DispatchFn,
    Effect,
    EffectKind,
    Event,
    EventStore,
    Result,
    RunResult,
    RuntimeCtx,
    Session,
    StepReport,
    Usage,
    checkpoint,
    restore_checkpoint,
)

__all__ = ["Runner", "InMemoryEventStore"]


class InMemoryEventStore(EventStore):
    """M1 自带 fake：内存 checkpoint 存储（M7 提供真实持久化）。"""

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {}

    def save(self, session_id: str, snapshot: dict[str, Any]) -> None:
        self.snapshots[session_id] = snapshot

    def load(self, session_id: str) -> dict[str, Any] | None:
        return self.snapshots.get(session_id)


@dataclass
class _Runtime:
    interpreter: InterpreterImpl
    last_value: Any = None
    step: int = 0


def _send_value(event: Event) -> Any:
    """把事件里的 Result 信封还原成 Program 当初 send 收到的值。"""
    values = [r.data if r.ok else r for r in event.results]
    if event.batched:
        return values
    return values[0] if values else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class Runner:
    def __init__(
        self,
        dispatch: DispatchFn,
        call: CallFn,
        *,
        store: EventStore | None = None,
        ctx_factory: Callable[[Effect, Session], Any] | None = None,
        checkpoint_every: int = 1,
        max_steps: int = 1000,
    ) -> None:
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
        self._runtimes: dict[str, _Runtime] = {}
        self._reasons: dict[str, str] = {}
        self._errors: dict[str, str | None] = {}

    # ------------------------------------------------------------------ #
    # 内部：运行时状态 / 重放 / 单 Effect 执行
    # ------------------------------------------------------------------ #
    def _runtime(self, session: Session) -> _Runtime:
        rt = self._runtimes.get(session.session_id)
        if rt is None:
            rt = _Runtime(InterpreterImpl(session.program_factory()))
            self._runtimes[session.session_id] = rt
            self._replay(session, rt)
        return rt

    def _replay(self, session: Session, rt: _Runtime, answer: Any = None) -> None:
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
    def _bind(ctx: Any, runner: "Runner") -> Any:
        """让链路的洋葱中心能调用 M3 的 call（M2 不 import M3）。"""
        ctx.invoke = lambda effect, c: runner._call(effect, c)
        return ctx

    async def _execute(self, effect: Effect, ctx: Any, session: Session) -> tuple[Result, tuple[Event, ...]]:
        if effect.kind is EffectKind.CHILD:
            return await self._child(effect, session)
        try:
            outcome = await _maybe_await(self._dispatch(effect, ctx))
            if outcome is None:  # 最小 / fake dispatch：Runner 自己调 call
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
        except Exception as exc:  # 异常绝不抛穿运行时，交 M2 回程 filter / StepReport
            return Result.failure("PORT_CRASHED", f"{type(exc).__name__}: {exc}"), ()

    async def _child(self, effect: Effect, session: Session) -> tuple[Result, tuple[Event, ...]]:
        """Effect(child) 递归创建子 Session，复用同一解释器与注入函数。"""
        payload = effect.payload
        factory = payload.get("program_factory")
        if factory is None:
            program = payload.get("program")
            if program is None:
                return Result.failure("CHILD_PROGRAM_MISSING", "child needs program_factory"), ()
            def factory(p: Any = program) -> Any:
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
        batch: Any = None
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
            except Exception as exc:  # pragma: no cover - 防御：任何 program 崩溃都进报告
                status, error, reason = "error", "PROGRAM_CRASHED", str(exc)
                batch = EffectBatch(())

            if status == "running" and batch is DONE:
                status, reason = "done", "program_returned"
                effects = (Effect(EffectKind.END, {"reason": reason}),)
                results = (Result.success(None),)
            elif status == "running":
                assert isinstance(batch, EffectBatch)
                effects = tuple(
                    replace(e, trace_id=f"{session.session_id}:{step_no:04d}:{i:02d}")
                    for i, e in enumerate(batch.effects)
                )
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
                    ctxs = [self._bind(self._ctx_factory(e, session), self) for e in effects]
                    executed = await asyncio.gather(
                        *(self._execute(e, c, session) for e, c in zip(effects, ctxs))
                    )
                    results = tuple(item[0] for item in executed)
                    child_events = tuple(ev for item in executed for ev in item[1])
                    for ev in child_events:
                        session.events.append(ev)
                    if any(not r.ok and r.error and r.error.code == "CHAIN_ABORTED" for r in results):
                        status, reason = "error", "chain aborted"
                        error = next(
                            r.error.message for r in results
                            if not r.ok and r.error and r.error.code == "CHAIN_ABORTED"
                        )
                    rt.last_value = [r.data if r.ok else r for r in results] if batch.batched else (
                        results[0].data if results and results[0].ok else (results[0] if results else None)
                    )

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

    async def resume(self, session: Session, answer: Any) -> RunResult:
        """从挂起断点继续：答案 send 回 Program，事件日志不动（跨周期恢复入口）。"""
        rt = _Runtime(InterpreterImpl(session.program_factory()))
        self._runtimes[session.session_id] = rt
        self._replay(session, rt, answer=answer)
        return await self._loop(session, initial_value=answer)

    async def _loop(self, session: Session, initial_value: Any = None) -> RunResult:
        rt = self._runtime(session)
        if initial_value is not None:
            rt.last_value = initial_value
        for _ in range(self._max_steps):
            report = await self.step(session)
            if report.status != "running":
                break
        return self._result(session)

    def _result(self, session: Session) -> RunResult:
        last = session.events[-1] if session.events else None
        status = last.status if last else "running"
        return RunResult(
            session_id=session.session_id,
            state=session.state,
            status=status,
            events=tuple(session.events),
            output=session.state.get("output"),
            reason=self._reasons.get(session.session_id, ""),
            error=self._errors.get(session.session_id),
        )

    def replay(self, session: Session, initial_state: AgentState) -> AgentState:
        """事件溯源重放：从初始 State 逐步 fold 得到终态（M1-T03）。"""
        return replay_events(session.events, initial_state)

    def load(self, session_id: str, program_factory: Callable[[], Any]) -> Session:
        """从 checkpoint 恢复 Session（新进程 / 跨周期断点恢复）。"""
        snap = self._store.load(session_id)
        if snap is None:
            raise PortMissing(f"no checkpoint for session {session_id}")
        return restore_checkpoint(snap, program_factory)
