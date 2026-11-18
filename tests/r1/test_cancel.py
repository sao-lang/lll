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

    def test_runner_cancel_stops_mid_run(self) -> None:
        """B7：运行中 cancel() 必须能被 step 检查看到并停止后续 Effect。

        CancellationToken frozen 的语义不变（cancel() 返回新对象），
        Runner 用内部可变 holder 接收运行期取消，兑现 §4.1"每步检查"承诺。
        """
        def many_effects():
            for _ in range(5):
                yield Effect(EffectKind.LLM, {"messages": []})
            yield Effect(EffectKind.END, {"output": "done"})

        runner = Runner(dispatch=_dispatch, call=_call, cancellation=CancellationToken())
        session = Session(session_id="s-mid", program_factory=many_effects)
        # 先跑一步（未取消，正常执行一个 Effect）
        first = asyncio.run(runner.step(session))
        assert first.status == "running"
        # 运行中取消
        runner.cancel("user stop mid-run")
        second = asyncio.run(runner.step(session))
        assert second.status == "cancelled"
        assert second.reason == "user stop mid-run"
        assert second.effects == ()
        # RunResult 也带取消原因（B6）
        result = runner._result(session)
        assert result.reason == "user stop mid-run"
