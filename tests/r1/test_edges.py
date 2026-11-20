"""R1 补充：解释器边缘分支、CHILD、checkpoint load、错误折叠。"""
import asyncio

import pytest

from runtime import AgentState, Budgets, DONE, DispatchFn, Effect, Message, PortError, ProgramInvalid, Result, Runner, Session, Usage, InterpreterImpl, EffectBatch, PortMissing, ProgramCrashed, restore_checkpoint, Invoke, Spawn, Terminate

# ---------- 解释器 ----------
def _gen_with(effects):
    def gen():
        for e in effects:
            yield e
    return gen()

def test_interpreter_expands_empty_batch():
    it = InterpreterImpl(_gen_with([Invoke(port="tool", payload={'calls': []}, batch=True)]))
    batch = it.send(None)
    assert isinstance(batch, EffectBatch) and len(batch.effects) == 1
    assert batch.effects[0].batch is False

def test_interpreter_yields_list():
    def gen():
        yield [Invoke(port="tool", payload={}), Invoke(port="tool", payload={})]
    it = InterpreterImpl(gen())
    batch = it.send(None)
    assert batch.batched is True and len(batch.effects) == 2

def test_interpreter_invalid_yield():
    def bad():
        yield "not an effect"
    with pytest.raises(ProgramInvalid):
        InterpreterImpl(bad()).send(None)

def test_interpreter_crash_wrapped():
    def crashing():
        raise RuntimeError("boom")
        yield
    with pytest.raises(ProgramCrashed):
        InterpreterImpl(crashing()).send(None)

def test_interpreter_done():
    def empty():
        return
        yield
    assert InterpreterImpl(empty()).send(None) is DONE

async def fake_call(effect, ctx):
    return Result.success("called")

# ---------- CHILD 递归 ----------
def test_child_session():
    async def run():
        def child(state):
            yield Terminate(output='child-done')
        def parent(state):
            v = yield Spawn(program_factory=lambda: child(AgentState()))
            yield Terminate(output=v)
        runner = Runner(call=fake_call)
        s = Session(session_id="p", program_factory=lambda: parent(AgentState()))
        r = await runner.run(s)
        assert r.output == "child-done"
    asyncio.run(run())

def test_child_missing_program():
    async def run():
        def parent(state):
            v = yield Spawn()
            yield Terminate(output=v.error.code if hasattr(v, 'error') else 'x')
        runner = Runner(call=fake_call)
        s = Session(session_id="pm", program_factory=lambda: parent(AgentState()))
        r = await runner.run(s)
        assert r.output == "CHILD_PROGRAM_MISSING"
    asyncio.run(run())

# ---------- checkpoint load ----------
def test_load_checkpoint():
    async def run():
        def program(state):
            yield Terminate(output='done')
        store_runner = Runner(call=fake_call, checkpoint_every=1)
        s = Session(session_id="loadme", program_factory=lambda: program(AgentState()))
        await store_runner.run(s)
        new_runner = Runner(call=fake_call, store=store_runner._store)
        loaded = await new_runner.load("loadme", program)
        assert loaded.state.output == "done"
        with pytest.raises(PortMissing):
            await new_runner.load("missing", program)
    asyncio.run(run())

def test_run_already_terminal():
    async def run():
        def program(state):
            yield Terminate(output='done')
        runner = Runner(call=fake_call)
        s = Session(session_id="t", program_factory=lambda: program(AgentState()))
        from runtime import Event
        s.events.append(Event(0, "t", 0, AgentState(), (), (), status="done"))
        r = await runner.run(s)
        assert r.status == "done"
    asyncio.run(run())

# ---------- 更多 runner 分支（稳健覆盖） ----------
def test_parallel_limit_forces_end():
    async def run():
        def program(state):
            # 一步产出 2 个 Effect，超过 max_parallel_children=1，触发 check_parallel 强制 END
            yield [Invoke(port="llm", payload={'messages': []}), Invoke(port="llm", payload={'messages': []})]
        budgets = Budgets(max_parallel_children=1)
        runner = Runner(call=fake_call)
        s = Session(session_id="pl", program_factory=lambda: program(AgentState(budgets=budgets)),
                    state=AgentState(budgets=budgets))
        r = await runner.run(s)
        assert r.status == "done" and "parallel" in r.reason, r.reason
    asyncio.run(run())

def test_replay_with_batched_events():
    async def run():
        def program(state):
            results = yield Invoke(port="tool", payload={'calls': [{'call_id': '1'}]}, batch=True)
            yield Terminate(output=len(results))
        runner = Runner(call=fake_call)
        s = Session(session_id="rb", program_factory=lambda: program(AgentState()))
        r = await runner.run(s)
        assert r.output == 1
    asyncio.run(run())

def test_checkpoint_every_multiple():
    async def run():
        def program(state):
            yield Invoke(port="llm", payload={})
            yield Invoke(port="llm", payload={})
            yield Terminate(output='ok')
        runner = Runner(call=fake_call, checkpoint_every=2)
        s = Session(session_id="ce", program_factory=lambda: program(AgentState()))
        r = await runner.run(s)
        assert r.status == "done"
        assert await runner._store.load("ce") is not None
    asyncio.run(run())

def test_max_steps_stops():
    async def run():
        def infinite(state):
            while True:
                yield Invoke(port="llm", payload={})
        runner = Runner(call=fake_call, max_steps=3)
        s = Session(session_id="ms", program_factory=lambda: infinite(AgentState()))
        await runner.run(s)
        assert len(s.events) <= 3
    asyncio.run(run())

def test_replay_events_helper():
    from runtime import replay_events, Event
    state = AgentState()
    effect = Terminate(output='x')
    event = Event(0, "s", 0, state, (effect,), (Result.success(None),))
    result = replay_events([event], state)
    assert result.output == "x"
