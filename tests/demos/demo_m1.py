"""M1 独立闭环 demo：fake dispatch/call 跑通 ReAct + 确定性 + 回放 + checkpoint。

运行：make demo-m1   （或 .venv/Scripts/python tests/demos/demo_m1.py）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from runtime import (  # noqa: E402
    AgentState, Effect, EffectKind, Runner, Session, fingerprint,
)
from tests.fakes import FakeCall, FakeDispatch, llm_response  # noqa: E402


def react_program(state: AgentState):
    """用户 Agent 逻辑：想 → 动 → 想 → 结束（纯函数，无任何 IO / 时间 / 随机）。"""
    while True:
        resp = yield Effect(EffectKind.LLM, {"messages": [m.to_dict() for m in state.messages]})
        if not getattr(resp, "tool_calls", None):
            yield Effect(EffectKind.END, {"output": resp.content})
            return
        yield Effect(EffectKind.TOOL, {"calls": resp.tool_calls}, batch=True)


def make_session() -> tuple[Session, FakeCall]:
    call = FakeCall([
        llm_response("thinking", [{"call_id": "c1", "name": "echo", "args": {"text": "hi"}}]),
        llm_response("done"),
    ])
    session = Session(session_id="demo-m1", program_factory=lambda: react_program(AgentState()))
    return session, call


async def main() -> None:
    session, call = make_session()
    runner = Runner(dispatch=FakeDispatch(), call=call)
    result = await runner.run(session)

    print(f"[M1] status={result.status} output={result.output!r} steps={len(result.events)}")
    for ev in result.events:
        print(f"  step {ev.step}: {[e.kind.value for e in ev.effects]} -> {ev.status}")

    # 确定性：同输入重跑，事件日志指纹逐字节一致
    session2, call2 = make_session()
    result2 = await Runner(dispatch=FakeDispatch(), call=call2).run(session2)
    f1, f2 = fingerprint(result.events), fingerprint(result2.events)
    print(f"[M1] fingerprint = {f1[:16]}... identical = {f1 == f2}")

    # 回放：从初始 State 重放事件日志，终态全等
    replayed = runner.replay(session, AgentState())
    print(f"[M1] replay state equals final = {replayed == result.state}")

    # checkpoint：挂起/每步必存，可跨周期恢复
    snap = runner._store.load("demo-m1")
    restored = runner.load("demo-m1", lambda: react_program(AgentState()))
    print(f"[M1] checkpoint saved={snap is not None} restored_events={len(restored.events)}")

    assert result.status == "done" and result.output == "done"
    assert f1 == f2
    assert replayed == result.state
    assert restored.state == result.state
    print("[M1] PASS: 解释循环 / 确定性 / 回放 / checkpoint 全部闭环")


if __name__ == "__main__":
    asyncio.run(main())
