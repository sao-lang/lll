"""R1-A5 取消传播测试：令牌置位后内核停止执行 + 子 Agent 继承 + 事件溯源。"""
from __future__ import annotations

import asyncio

from hooks import Decision, HookCtx
from runtime import (
    AgentState,
    CancellationToken,
    Effect,
    EffectKind,
    Result,
    Runner,
    RuntimeCtx,
    Session,
)


def _program():
    """有限程序：产出一个 Effect 后 END（验证取消时不进入循环）。"""
    yield Effect(EffectKind.LLM, {"messages": []})
    yield Effect(EffectKind.END, {"output": "done"})


async def _call(effect: Effect, ctx) -> Result:
    return Result.success(data="ok")


async def _dispatch(effect: Effect, ctx: HookCtx) -> Decision:
    return Decision.continue_()


class TestCancellationToken:
    def test_cancel_marks_reason(self) -> None:
        token = CancellationToken()
        cancelled = token.cancel("user stop")
        assert cancelled.cancelled and cancelled.reason == "user stop"
        assert not token.cancelled  # 不可变：原令牌不变

    def test_runner_stops_when_pre_cancelled(self) -> None:
        """执行前已取消：内核不执行任何 Effect，写 cancelled 事件。"""
        token = CancellationToken().cancel("user stop")
        runner = Runner(dispatch=_dispatch, call=_call, cancellation=token)
        session = Session(session_id="s-cancel", program_factory=lambda: _program())
        result = asyncio.run(runner.step(session))
        assert result.status == "cancelled"
        assert result.reason == "user stop"
        assert any(e.status == "cancelled" for e in session.events)
        assert result.effects == ()  # 未执行任何 Effect

    def test_child_inherits_parent_token(self) -> None:
        """子 Agent 派生时继承父取消令牌（递归传播）。"""
        token = CancellationToken().cancel("parent stop")
        runner = Runner(dispatch=_dispatch, call=_call, cancellation=token)
        ctx = RuntimeCtx(effect=Effect(EffectKind.LLM, {}), state=AgentState(), session_id="s")
        bound = runner._bind(ctx, runner)
        assert bound.cancellation is token
