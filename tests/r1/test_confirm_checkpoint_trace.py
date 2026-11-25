"""确认 bug #5：checkpoint 恢复丢 Effect trace_id 导致指纹漂移（audit_fp_diff.py）。"""
from __future__ import annotations

import asyncio

from runtime import (
    AgentState,
    Event,
    Invoke,
    Result,
    Runner,
    Session,
    Spawn,
    Suspend,
    Terminate,
    Usage,
    canonical,
    checkpoint,
    fingerprint,
    restore_checkpoint,
)


def test_all_four_effect_trace_ids_survive_checkpoint_roundtrip() -> None:
    """四种 Effect 的 trace_id 经 plain→DTO 往返后必须逐字保留。"""
    effects = (
        Invoke(port="llm", payload={}, trace_id="s:0000:00"),
        Spawn(trace_id="s:0000:01"),
        Suspend(question="q", trace_id="s:0000:02"),
        Terminate(output="x", reason="r", trace_id="s:0000:03"),
    )
    results = tuple(Result.success(None) for _ in effects)
    event = Event(
        seq=1, session_id="s-trace", step=1, state_snapshot=AgentState(),
        effects=effects, results=results, status="done",
    )
    session = Session(session_id="s-trace", program_factory=lambda: iter(()), events=(event,))

    snap = checkpoint(session)
    restored = restore_checkpoint(snap, lambda: iter(()))

    got = tuple(e.trace_id for e in restored.events[0].effects)
    assert got == ("s:0000:00", "s:0000:01", "s:0000:02", "s:0000:03"), \
        f"恢复后 trace_id 序列={got!r}，期望四字串全保留"
    assert canonical(restored.events[0]) == canonical(session.events[0]), \
        "恢复后事件 canonical 必须与原始逐字相等"
    assert fingerprint(restored.events) == snap["fingerprint"]


def test_run_checkpoint_restore_fingerprint_is_stable() -> None:
    """端到端：真实 run 产出的快照恢复后指纹必须与原快照、原会话完全相等。"""
    async def run() -> None:
        def program():
            yield Invoke(port="llm", payload={"p": 1})
            yield Terminate(output="F")

        session = Session(session_id="fp", program_factory=program)
        runner = Runner(call=lambda e, c: Result.success("x", usage=Usage(prompt_tokens=1)))
        await runner.run(session)

        snap = checkpoint(session)
        restored = restore_checkpoint(snap, program)

        assert fingerprint(restored.events) == snap["fingerprint"], \
            f"恢复指纹={fingerprint(restored.events)}, 快照指纹={snap['fingerprint']}"
        assert fingerprint(restored.events) == fingerprint(session.events)
        assert restored.events[1].effects[0].trace_id == "fp:0001:00"

    asyncio.run(run())
