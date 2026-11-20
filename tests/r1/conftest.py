"""R1 测试夹具：fake dispatch/call 与 program 工厂（模块内闭环，不依赖外部服务）。"""
from __future__ import annotations

import pytest

from runtime import AgentState, Effect, Result, Session, Invoke, Terminate


def react_program(state: AgentState):
    """ReAct 脚本：LLM → 有 tool_calls 则调工具 → 无则 END。"""
    while True:
        resp = yield Invoke(port="llm", payload={'messages': state.messages})
        calls = resp.get("tool_calls", []) if isinstance(resp, dict) else []
        if not calls:
            yield Terminate(output=resp.get('content', 'done') if isinstance(resp, dict) else 'done')
            return
        yield Invoke(port="tool", payload={'calls': calls}, batch=True)


async def fake_call(effect: Effect, ctx) -> Result:
    """最小 Port：LLM 返回可配置响应，工具回显。"""
    if isinstance(effect, Invoke) and effect.port == "llm":
        return Result.success({"content": "answer", "tool_calls": []})
    if isinstance(effect, Invoke) and effect.port == "tool":
        return Result.success({"ok": True, "value": "x"})
    return Result.success(None)


async def fake_dispatch(effect: Effect, ctx) -> None:
    """最小 dispatch：不干预，返回 None 让 Runner 兜底调 call。"""
    return None


@pytest.fixture
def make_session():
    """会话工厂：固定 session_id 便于确定性比对。"""
    def _make(program=react_program, sid="s1", state=None):
        init = state or AgentState()
        return Session(session_id=sid, program_factory=lambda: program(init))
    return _make
