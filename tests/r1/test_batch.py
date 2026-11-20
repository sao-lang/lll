"""R1-T05：batch 并行执行、结果按序回填。"""
import asyncio

from runtime import AgentState, Effect, Result, Runner, Session, Invoke, Terminate


async def fake_call(effect, ctx):
    call = effect.payload.get("call", {})
    return Result.success({"ok": True, "value": call})


async def fake_dispatch(effect, ctx):
    return None


def batch_program(state):
    calls = [{"call_id": "1"}, {"call_id": "2"}, {"call_id": "3"}]
    results = yield Invoke(port="tool", payload={'calls': calls}, batch=True)
    yield Terminate(output=[r['value']['call_id'] for r in results])


def test_batch_ordered_results():
    async def run():
        runner = Runner(call=fake_call)
        session = Session(session_id="batch", program_factory=lambda: batch_program(AgentState()))
        result = await runner.run(session)
        assert result.status == "done"
        assert result.output == ["1", "2", "3"]
    asyncio.run(run())
