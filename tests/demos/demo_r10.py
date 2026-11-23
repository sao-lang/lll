"""R10 独立闭环 demo：Trace 串联 + 事件流续读 + 回放 checkpoint + 确定性评测。

运行：.venv/Scripts/python tests/demos/demo_r10.py
不联网、不调真实 LLM（录制/确定性回注）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import PortCtx  # noqa: E402
from harness.durability import CheckpointManager  # noqa: E402
from harness.eval import EvalCase, GoldenJudge, RuleEval  # noqa: E402
from harness.observability import (  # noqa: E402
    EventEnvelope, InMemoryEventStream, NoopTelemetry, TimelineProjection, Trace,
)
from runtime import Budgets  # noqa: E402


def _plain_event(output: str) -> dict:
    """与 R1 JsonSnapshot 同构的 plain dict 事件（真实落盘形态，非内存对象假绿）。"""
    return {
        "seq": 0, "session_id": "demo-s10", "step": 0,
        "state_snapshot": {
            "messages": [], "scratch": {}, "version": "0.0.0", "output": None,
            "budgets": Budgets().__dict__, "last_skill": None, "last_child": None,
            "last_wait_human": None, "last_step_kinds": [], "last_tokens": 0,
            "last_reason": "",
        },
        "effects": [{"output": output, "reason": "", "disposition": "terminate"}],
        "results": [{"ok": True, "data": None, "usage": None, "error": None}],
        "batched": False, "ts": 0, "cost_usd": 0, "tokens": 0,
        "status": "done", "reason": "terminated",
    }


def main() -> None:
    """演示四类能力闭环。"""
    telemetry = NoopTelemetry()
    trace = Trace(trace_id="demo-t1", telemetry=telemetry)
    with trace.span("model.call", component="model"):
        with trace.span("tool.call", component="tools"):
            pass
    print(f"[R10] trace spans={[s.name for s in trace.spans()]}")

    stream = InMemoryEventStream()
    for i in (1, 2):
        stream.publish(EventEnvelope(seq=i, topic="trace.span", ts=float(i),
                                     type="span", data={"i": i}))

    async def drain():
        return [e async for e in stream.subscribe("trace.span")]
    events = asyncio.run(drain())
    print(f"[R10] stream seqs={[e.seq for e in events]} "
          f"timeline={TimelineProjection().project(events).data['count']}")

    mgr = CheckpointManager()
    from runtime import AgentState
    snap = mgr.snapshot("demo-s10", AgentState(), (_plain_event("final-answer"),))  # type: ignore[arg-type]
    print(f"[R10] checkpoint fingerprint={snap.fingerprint}")
    from harness.durability import replay_snapshot
    print(f"[R10] replay output={replay_snapshot(snap).output!r}")

    evaluator = RuleEval(cases={"c1": EvalCase(case_id="c1", input="q",
                                               expected="a", expected_trajectory=("s1",))})
    score = asyncio.run(evaluator.score("c1", "a", PortCtx()))
    judge_score = asyncio.run(GoldenJudge().score(
        EvalCase(case_id="c1", input="q", expected="a",
                 expected_trajectory=("s1",)), ["s1"], PortCtx()))
    print(f"[R10] eval score={score} judge={judge_score.value}")
    print("[R10] PASS: Trace 串联 / 事件流续读 / 回放 checkpoint / 确定性评测闭环")


if __name__ == "__main__":
    main()
