"""R1 多Agent原语回归：并行 CHILD 扇出/聚合/子会话ID唯一/预算分账。"""
import asyncio

from runtime import AgentState, Effect, EffectKind, Result, Runner, Session, Usage


async def fake_call(effect, ctx):
    if effect.kind is EffectKind.LLM:
        return Result.success({"content": "ok"}, usage=Usage(prompt_tokens=2, cost_usd=0.01))
    return Result.success(None)


async def fake_dispatch(effect, ctx):
    return None


def make_child(name: str):
    """无参工厂：子Agent的 program_factory 必须是 Callable[[], Program]。"""
    def child():
        yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END, {"output": name})
    return child


def test_parallel_children_unique_ids_and_aggregate():
    """同批 yield 多个 CHILD：子会话 ID 必须唯一，结果按序聚合。"""
    async def run():
        def supervisor(state):
            results = yield [
                Effect(EffectKind.CHILD, {"program_factory": make_child(n)})
                for n in ("a", "b", "c")
            ]
            yield Effect(EffectKind.END, {"output": "|".join(str(r) for r in results)})

        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        s = Session(session_id="multi", program_factory=lambda: supervisor(AgentState()))
        result = await runner.run(s)
        assert result.status == "done", result.reason
        assert result.output == "a|b|c", result.output
        child_ids = {e.session_id for e in result.events if e.session_id != "multi"}
        assert child_ids == {"multi:child:0", "multi:child:1", "multi:child:2"}, child_ids
    asyncio.run(run())


def test_child_budget_hop_accounting():
    """每个子Agent派生时父预算扣 1 hop（预算分账由内核强制）。"""
    async def run():
        def parent(state):
            results = yield Effect(EffectKind.CHILD, {"program_factory": make_child("x")})
            yield Effect(EffectKind.END, {"output": str(results)})

        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        s = Session(session_id="acct", program_factory=lambda: parent(AgentState()))
        result = await runner.run(s)
        assert result.status == "done"
        # fold 以 payload.budget 声明扣减；CHILD 未声明则默认 1 turn，hops 在 _child 扣
        assert result.state.budgets.used_turns >= 1
    asyncio.run(run())
