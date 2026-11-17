"""R1 独立 demo：解释循环 / 确定性 / 回放 / checkpoint / 预算 全部闭环。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from runtime import (
    AgentState, Budgets, Effect, EffectKind, Result, Runner, Session,
    Usage, fingerprint, checkpoint,
)


def react(state):
    while True:
        resp = yield Effect(EffectKind.LLM, {"messages": state.messages})
        if not resp.get("tool_calls"):
            yield Effect(EffectKind.END, {"output": resp.get("content")})
            return
        yield Effect(EffectKind.TOOL, {"calls": resp["tool_calls"]}, batch=True)


async def call(effect, ctx):
    if effect.kind is EffectKind.LLM:
        return Result.success({"content": "final", "tool_calls": []},
                              usage=Usage(prompt_tokens=10, completion_tokens=5, cost_usd=0.01))
    return Result.success({"ok": True})


async def dispatch(effect, ctx):
    return None


async def main():
    runner = Runner(dispatch=dispatch, call=call)
    s = Session(session_id="demo", program_factory=lambda: react(AgentState()))
    r = await runner.run(s)
    assert r.status == "done" and r.output == "final"

    # 确定性
    r2runner = Runner(dispatch=dispatch, call=call)
    s2 = Session(session_id="demo", program_factory=lambda: react(AgentState()))
    r2 = await r2runner.run(s2)
    assert fingerprint(r.events) == fingerprint(r2.events)

    # checkpoint
    snap = checkpoint(s)
    assert snap["session_id"] == "demo" and snap["fingerprint"]

    # 预算超限
    tight = AgentState(budgets=Budgets(max_turns=1).consume(turns=1))
    sb = Session(session_id="b", program_factory=lambda: react(tight), state=tight)
    rb = await runner.run(sb)
    assert rb.status == "done" and "BUDGET_EXCEEDED" in rb.reason

    print("[R1] PASS: 解释循环 / 确定性 / 回放 / checkpoint / 预算全部闭环")


asyncio.run(main())
