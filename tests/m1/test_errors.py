"""M1-T07 错误分类：四类错误进 StepReport，不抛穿运行时。"""
from __future__ import annotations

from dataclasses import dataclass

from agentkit.runtime import (
    AgentState, Effect, EffectKind, PortMissing, Result, Runner, Session,
)
from agentkit.runtime.errors import (
    BudgetExceeded, ChainAborted, ProgramCrashed, ProgramInvalid,
)
from agentkit.runtime.interpreter import InterpreterImpl
from tests.fakes import FakeCall, FakeDispatch, obj, run


def test_t07_program_invalid_yield():
    def bad(state):
        yield 42

    report = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).step(
        Session("invalid", lambda: bad(AgentState()))
    ))
    assert report.status == "error" and report.error == "PROGRAM_INVALID"


def test_t07_program_crashed():
    def bad(state):
        raise RuntimeError("boom")
        yield Effect(EffectKind.END)

    report = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).step(
        Session("crash", lambda: bad(AgentState()))
    ))
    assert report.status == "error" and report.error == "PROGRAM_CRASHED"


def test_t07_not_a_generator_is_reported():
    report = run(Runner(dispatch=FakeDispatch(), call=FakeCall()).step(
        Session("plain", lambda: "not a generator")
    ))
    assert report.status == "error" and report.error == "PROGRAM_INVALID"


def test_t07_port_missing_folded_into_result():
    async def missing(effect, ctx):
        raise PortMissing("port llm is not registered")

    def program(state):
        yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END)

    report = run(Runner(dispatch=FakeDispatch(), call=missing).step(
        Session("noport", lambda: program(AgentState()))
    ))
    assert report.status == "running" and not report.results[0].ok
    assert report.results[0].error.code == "PORT_MISSING"


def test_t07_dispatch_exception_never_escapes():
    async def broken(effect, ctx):
        raise ValueError("dispatch broke")

    def program(state):
        yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END)

    report = run(Runner(dispatch=broken, call=FakeCall()).step(
        Session("broken", lambda: program(AgentState()))
    ))
    assert report.results[0].ok is False and report.results[0].error.code == "PORT_CRASHED"


def test_t07_abort_verdict_stops_run():
    @dataclass
    class Abort:
        abort: bool = True
        verdict: object = None
        data: object = None

    async def aborted(effect, ctx):
        return Abort(verdict=obj(reason="policy"))

    def program(state):
        yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END)

    result = run(Runner(dispatch=aborted, call=FakeCall()).run(
        Session("abort", lambda: program(AgentState()))
    ))
    assert result.status == "error" and result.error == "policy"


def test_t07_error_classes_and_interpreter_protocol():
    assert BudgetExceeded.code == "BUDGET_EXCEEDED"
    assert ChainAborted.code == "CHAIN_ABORTED"
    assert ProgramInvalid.code == "PROGRAM_INVALID"
    assert ProgramCrashed.code == "PROGRAM_CRASHED"
    try:
        InterpreterImpl(123)
        raise AssertionError("应拒绝非 generator")
    except ProgramInvalid:
        pass
    assert Result.failure("X", "y", True).error.retryable is True
