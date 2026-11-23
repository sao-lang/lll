"""RED：真实 Runner→checkpoint→save→load→replay 恢复终态（全模块体检 B6）。

现有 test_replay.py 用内存 Ev 对象（真属性）自证，掩盖了 load() 返回 plain dict 的现实。
本测试走真实链路：Runner 跑出真实事件 → checkpoint() → CheckpointManager.save
→ load（plain dict）→ replay_snapshot，断言 output 真恢复。当前实现应 FAIL。
"""
from __future__ import annotations

import asyncio

from runtime import (
    AgentState, InMemoryEventStore, Invoke, Result, Runner, Session, Terminate,
    checkpoint,
)
from harness.durability import CheckpointManager, replay_snapshot


def _program() -> object:
    def react(state: AgentState) -> object:
        yield Terminate(output="real-answer")
    return react(AgentState())


def _real_run() -> tuple[Session, object]:
    """真实 Runner 跑出一个 done 的 Session + 其 JsonSnapshot。"""
    async def call(effect: object, ctx: object) -> Result:
        return Result.success({"ok": True})

    runner = Runner(call=call)
    session = Session(session_id="s-red", program_factory=lambda: _program())
    result = asyncio.run(runner.run(session))
    assert result.status == "done" and result.output == "real-answer"
    return session, checkpoint(session)


def test_red_real_load_then_replay_restores_output() -> None:
    """真实 checkpoint save→load（plain dict）→replay，output 必须恢复（当前 FAIL）。"""
    session, jsonsnap = _real_run()
    store = InMemoryEventStore()
    mgr = CheckpointManager(store=store)

    # 把真实 JsonSnapshot 写进 store（模拟落盘）
    asyncio.run(store.save("s-red", jsonsnap))

    # 真实 load：events 是 store 返回的 plain dict
    loaded = asyncio.run(mgr.load("s-red"))
    assert loaded is not None
    assert len(loaded.events) >= 1
    assert isinstance(loaded.events[0], dict), "load 应返回 plain dict（真实跨进程语义）"

    # replay 后必须恢复终态 output
    final = replay_snapshot(loaded)
    assert final.output == "real-answer", (
        f"replay 应恢复 output='real-answer'，实得 {final.output!r}"
    )
