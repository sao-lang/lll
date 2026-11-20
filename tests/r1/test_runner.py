"""R1-T01：解释循环单步顺序。"""
import asyncio

from runtime import AgentState, Invoke, Runner, Session
from tests.r1.conftest import fake_call, fake_dispatch, react_program


def test_step_executes_in_order():
    async def run():
        runner = Runner(call=fake_call)
        init = AgentState()
        session = Session(session_id="s1", program_factory=lambda: react_program(init))
        report = await runner.step(session)
        assert report.step == 0
        assert len(report.effects) >= 1
        assert isinstance(report.effects[0], Invoke) and report.effects[0].port == "llm"
        assert len(report.results) == 1
        assert len(report.events) == 1
    asyncio.run(run())


def test_run_reaches_done():
    async def run():
        runner = Runner(call=fake_call)
        init = AgentState()
        session = Session(session_id="s2", program_factory=lambda: react_program(init))
        result = await runner.run(session)
        assert result.status == "done"
        assert result.output == "answer"
    asyncio.run(run())
