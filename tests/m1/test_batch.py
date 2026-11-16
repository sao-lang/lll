"""M1-T05 batch 并行：展开、并行执行、结果按序回填。"""
from __future__ import annotations

from runtime import AgentState, Effect, EffectKind, Result, Runner, Session, Usage
from runtime.interpreter import InterpreterImpl
from tests.fakes import obj, run


def test_t05_batch_results_keep_order():
    def program(state):
        results = yield Effect(EffectKind.TOOL, {
            "calls": [{"call_id": f"c{i}"} for i in range(3)]
        }, batch=True)
        yield Effect(EffectKind.END, {"output": [r.call_id for r in results]})

    async def call(effect, ctx):
        call_id = effect.payload["call"]["call_id"]
        return Result.success(obj(call_id=call_id, ok=True, value=call_id, error=None))

    result = run(Runner(dispatch=lambda e, c: None, call=call).run(
        Session("batch", lambda: program(AgentState()))
    ))
    assert result.output == ["c0", "c1", "c2"]
    assert len(result.events[0].effects) == 3 and result.events[0].batched


def test_t05_list_of_effects_is_batched():
    def program(state):
        results = yield [Effect(EffectKind.LLM, {"i": 0}), Effect(EffectKind.LLM, {"i": 1})]
        yield Effect(EffectKind.END, {"output": len(results)})

    result = run(Runner(dispatch=lambda e, c: None, call=lambda e, c: Result.success("x")).run(
        Session("list", lambda: program(AgentState()))
    ))
    assert result.output == 2 and result.events[0].batched


def test_t05_empty_batch_and_single_effect():
    interp = InterpreterImpl((lambda: (yield Effect(EffectKind.TOOL, {"calls": []}, batch=True)))())
    batch = interp.send(None)
    assert len(batch.effects) == 1 and batch.effects[0].batch is False

    def single(state):
        value = yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END, {"output": value})

    result = run(Runner(dispatch=lambda e, c: None, call=lambda e, c: Result.success("one", Usage(1, 1, 0.1))).run(
        Session("single", lambda: single(AgentState()))
    ))
    assert result.output == "one" and result.events[0].batched is False
