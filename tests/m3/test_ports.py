"""M3-T01 Port 协议族与 pydantic DTO 契约。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ports import (
    PORT_PROTOCOLS, BUILTIN_PORTS, DangerLevel, LLMRequest, LLMResponse,
    LLMPort, PortCtx, PortRegistry, ToolCall, ToolPort, ToolResult, ToolSpec,
    port_ctx_from,
)
from ports.adapters import InMemoryToolRegistry, JsonlTelemetry
from ports.adapters.fakes import ScriptedLLM
from runtime import Message, Usage


def test_t01_dto_validation_and_conversion():
    spec = ToolSpec(name="echo", description="回显", danger=DangerLevel.SAFE)
    req = LLMRequest.model_validate({
        "messages": [{"role": "user", "content": "hi"}],
        "tools_catalog": [spec.model_dump()],
        "temperature": 0.2,
    })
    assert isinstance(req.messages[0], Message) and req.tools_catalog[0].name == "echo"
    assert LLMResponse(content="x").usage == Usage()
    assert ToolCall(call_id="c", name="echo").args == {}
    assert ToolResult(call_id="c", ok=True).meta == {}
    with pytest.raises(ValidationError):
        ToolSpec(name="x", unknown_field=1)
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"call_id": "c"})  # 缺 name


def test_t01_protocol_family_is_complete():
    assert len(PORT_PROTOCOLS) == 12 and set(PORT_PROTOCOLS) == BUILTIN_PORTS
    assert isinstance(ScriptedLLM(), LLMPort)
    assert isinstance(InMemoryToolRegistry(), ToolPort)


def test_t01_port_ctx_from_chain_ctx():
    from chains import ChainCtx
    from runtime import AgentState, Effect, EffectKind
    c = ChainCtx(Effect(EffectKind.LLM), AgentState(), "s1")
    c.trace.extend(["pipe:a", "filter:b", "middleware:c"])
    c.meta["run_id"] = "run-1"
    ctx = port_ctx_from(c)
    assert ctx.session_id == "s1" and ctx.run_id == "run-1"
    assert ctx.trace_span == "pipe:a|filter:b|middleware:c"
    assert PortCtx("s2").budget_left is None


def test_t01_registry_protocol_enforcement():
    from ports import PortContractError
    reg = PortRegistry()
    reg.register("llm", ScriptedLLM(), protocol=LLMPort)
    with pytest.raises(PortContractError, match="does not implement"):
        reg.register("tool", object(), protocol=ToolPort)
    assert JsonlTelemetry.name == "jsonl-telemetry"
