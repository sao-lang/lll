"""M3-T03 错误信封：异常 100% 折叠、retryable 标注、自定义 port 折叠路径。"""
from __future__ import annotations

from pydantic import ValidationError

from chains import ChainCtx
from ports import PortRegistry, call, classify_exception, make_call
from tests.fakes.m3 import FailingAdapter, InMemoryTelemetry, ScriptedLLM
from runtime import (
    AgentState, Effect, EffectKind, PortMissing, Result, Usage,
)
from tests.fakes import run


def ctx(**meta):
    c = ChainCtx(Effect(EffectKind.LLM), AgentState(), "errors")
    c.meta.update(meta)
    c.trace.extend(["pipe:p", "filter:f"])
    return c


def test_t03_classify_exception_matrix():
    assert classify_exception(PortMissing("x")).code == "PORT_MISSING"
    assert classify_exception(TimeoutError("t")).retryable is True
    assert classify_exception(ConnectionError("c")).retryable is True
    assert classify_exception(ValueError("v")).retryable is False
    try:
        from ports import ToolCall
        ToolCall.model_validate({"call_id": "c"})
    except ValidationError as exc:
        assert classify_exception(exc).code == "PORT_INVALID"

    class ServerError(Exception):
        def __init__(self):
            super().__init__("503")
            self.response = type("R", (), {"status_code": 503})()

    assert classify_exception(ServerError()).retryable is True


def test_t03_adapter_exception_folded_with_retryable():
    reg = PortRegistry().register("llm", FailingAdapter(TimeoutError("timeout")))
    out = run(call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg))
    assert not out.ok and out.error.code == "TimeoutError" and out.error.retryable


def test_t03_invalid_dto_folded_as_port_invalid():
    reg = PortRegistry().register("tool", object())
    out = run(call(Effect(EffectKind.TOOL, {"call": {"name": "x"}}), ctx(), reg))
    assert out.error.code == "PORT_INVALID" and not out.error.retryable


def test_t03_missing_registry_and_special_kinds():
    out = run(call(Effect(EffectKind.LLM, {"messages": []}), ctx()))
    assert out.error.code == "PORT_REGISTRY_MISSING"
    reg = PortRegistry()
    assert run(call(Effect(EffectKind.END), ctx(), reg)).ok
    assert run(call(Effect(EffectKind.CHILD), ctx(), reg)).error.code == "CHILD_HANDLED_BY_RUNNER"


def test_t03_custom_x_port_and_degrade():
    class CustomPort:
        def call(self, payload, pctx):
            return {"echo": payload}

    reg = PortRegistry().register("x-db", CustomPort())
    out = run(call(Effect(EffectKind.SKILL, {"port": "x-db", "q": 1}), ctx(), reg))
    assert out.ok and out.data == {"echo": {"q": 1}}  # port 字段只用于路由，不进业务 payload

    class NotCallable:
        pass
    reg2 = PortRegistry().register("x-empty", NotCallable())
    out2 = run(call(Effect(EffectKind.SKILL, {"port": "x-empty"}), ctx(), reg2))
    assert out2.error.code == "PORT_NOT_CALLABLE"

    # degrade 判决切换到备用 LLM port
    reg3 = (PortRegistry()
            .register("llm", FailingAdapter(TimeoutError("down")))
            .register("x-llm-backup", ScriptedLLM()))
    out3 = run(call(Effect(EffectKind.LLM, {"messages": []}), ctx(degrade_port="x-llm-backup"), reg3))
    assert out3.ok and out3.data.content == "ok"


def test_t03_telemetry_log_is_best_effort():
    tele = InMemoryTelemetry()
    reg = PortRegistry().register("llm", ScriptedLLM()).register("telemetry", tele)
    run(call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg))
    assert tele.events[-1]["components"] == ["pipe:p", "filter:f"]

    class BrokenTelemetry:
        def log(self, event):
            raise RuntimeError("telemetry down")

    reg.register("telemetry", BrokenTelemetry())
    out = run(call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg))
    assert out.ok  # 可观测故障不影响主流程


def test_t03_make_call_binds_registry():
    fn = make_call(PortRegistry().register("llm", ScriptedLLM()))
    assert run(fn(Effect(EffectKind.LLM, {"messages": []}), ctx())).ok
    assert isinstance(Result.success(None, Usage(1, 1, 0.1)).usage, Usage)
