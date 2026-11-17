"""R1-T03：回放——事件重放恢复 State 与终态全等。"""
import asyncio

from runtime import AgentState, Runner, Session
from tests.r1.conftest import fake_call, fake_dispatch, react_program


def test_replay_matches_terminal_state():
    async def run():
        runner = Runner(dispatch=fake_dispatch, call=fake_call)
        init = AgentState()
        session = Session(session_id="s1", program_factory=lambda: react_program(init))
        result = await runner.run(session)
        replayed = runner.replay(session, init)
        assert replayed.output == result.state.output
        assert replayed.budgets == result.state.budgets
    asyncio.run(run())
