"""R1-T02：确定性——相同输入两次 run 事件日志逐字节一致。"""
import asyncio

from runtime import AgentState, Runner, Session, fingerprint
from tests.r1.conftest import fake_call, fake_dispatch, react_program


def test_deterministic_event_log():
    async def run():
        r1 = Runner(dispatch=fake_dispatch, call=fake_call)
        r2 = Runner(dispatch=fake_dispatch, call=fake_call)
        s1 = Session(session_id="same", program_factory=lambda: react_program(AgentState()))
        s2 = Session(session_id="same", program_factory=lambda: react_program(AgentState()))
        a = await r1.run(s1)
        b = await r2.run(s2)
        assert fingerprint(a.events) == fingerprint(b.events)
    asyncio.run(run())
