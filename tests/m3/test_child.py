"""M3-T06 child 递归会话：子 Session trace 挂父日志 + 三模块集成。"""
from __future__ import annotations

from agentkit.chains import Chains, MountPoint
from agentkit.chains.builtins import AllowFilter
from agentkit.ports import (
    LLMResponse, PortRegistry, ToolCall, call, make_call,
)
from agentkit.ports.adapters.fakes import ScriptedLLM
from agentkit.ports.adapters.tools import InMemoryToolRegistry
from agentkit.runtime import (
    AgentState, Effect, EffectKind, Runner, Session, Usage,
)
from tests.fakes import FakeDispatch, run


def child(state):
    yield Effect(EffectKind.END, {"output": "child-done"})


def parent(state):
    answer = yield Effect(EffectKind.CHILD, {
        "program_factory": lambda: child(AgentState()), "state": AgentState(),
    })
    yield Effect(EffectKind.END, {"output": answer})


def test_t06_child_events_are_attached_to_parent_trace():
    runner = Runner(dispatch=FakeDispatch(), call=make_call(PortRegistry()))
    session = Session("parent", lambda: parent(AgentState()))
    result = run(runner.run(session))
    assert result.output == "child-done"
    child_events = [e for e in session.events if ":child:" in e.session_id]
    assert len(child_events) == 1 and child_events[0].state_snapshot.get("output") == "child-done"
    assert session.state.budgets.used_hops == 0  # hops 消耗记在子会话预算里


def test_t06_child_without_program_is_reported():
    def bad_parent(state):
        value = yield Effect(EffectKind.CHILD, {})
        yield Effect(EffectKind.END, {"output": str(value)})

    report = run(Runner(dispatch=FakeDispatch(), call=make_call(PortRegistry())).step(
        Session("bad-child", lambda: bad_parent(AgentState()))
    ))
    assert report.results[0].error.code == "CHILD_PROGRAM_MISSING"


def test_t06_child_accepts_inline_program_generator():
    def inline_parent(state):
        value = yield Effect(EffectKind.CHILD, {"program": child(AgentState())})
        yield Effect(EffectKind.END, {"output": value})

    result = run(Runner(dispatch=FakeDispatch(), call=make_call(PortRegistry())).run(
        Session("inline", lambda: inline_parent(AgentState()))
    ))
    assert result.output == "child-done"


def test_t06_end_to_end_react_with_real_ports():
    from agentkit.ports import DangerLevel, ToolSpec

    def react(state):
        resp = yield Effect(EffectKind.LLM, {"messages": [m.to_dict() for m in state.messages]})
        if not resp.tool_calls:
            yield Effect(EffectKind.END, {"output": resp.content})
            return
        results = yield Effect(EffectKind.TOOL, {"calls": [
            {"call_id": tc.call_id, "name": tc.name, "args": tc.args}
            for tc in resp.tool_calls
        ]}, batch=True)
        yield Effect(EffectKind.END, {"output": results[0].value})

    tools = InMemoryToolRegistry()
    tools.register(ToolSpec(name="echo", danger=DangerLevel.SAFE), lambda args, c: f"echo:{args['text']}")
    ports = (PortRegistry()
             .register("llm", ScriptedLLM([LLMResponse(
                 content=None,
                 tool_calls=[ToolCall(call_id="c1", name="echo", args={"text": "hi"})],
                 usage=Usage(4, 2, 0.002),
             )]))
             .register("tool", tools))
    ports.freeze(("llm", "tool"))
    chains = Chains().filter(AllowFilter("guard"), at=MountPoint.BEFORE_LLM).freeze(("llm", "tool"))
    runner = Runner(dispatch=chains.dispatch, call=make_call(ports), ctx_factory=chains.ctx_factory())
    result = run(runner.run(Session("react", lambda: react(AgentState()))))
    assert result.status == "done" and result.output == "echo:hi"
    assert result.state.budgets.used_cost_usd == 0.002
