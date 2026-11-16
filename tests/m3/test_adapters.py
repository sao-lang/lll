"""M3-T04/T05 四个默认适配器 + fake + 替换实现。"""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import httpx
import pytest

from chains import ChainCtx
from ports import (
    DangerLevel, LLMRequest, PortCtx, PortRegistry, ToolCall, ToolSpec, call,
)
from ports.adapters import (
    ConsoleHuman, InMemoryToolRegistry, JsonlTelemetry, NullTelemetry, OpenAICompatLLM,
)
from ports.adapters.fakes import (
    FailingAdapter, InMemoryTelemetry, ScriptedLLM, StaticHuman, StaticSkillPort,
)
from runtime import AgentState, Effect, EffectKind, Message, Usage
from tests.fakes import run


def ctx() -> ChainCtx:
    return ChainCtx(Effect(EffectKind.LLM), AgentState(), "adapters")


def openai_payload(*, tool_calls=None, status=200):
    return {
        "model": "m1",
        "choices": [{"message": {"content": "hello" if not tool_calls else None,
                                 "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_t04_openai_compat_llm_full_translation():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=openai_payload(tool_calls=[
            {"id": "c1", "function": {"name": "echo", "arguments": '{"text":"hi"}'}}
        ]))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            llm = OpenAICompatLLM(base_url="http://fake/v1", api_key="KEY", model="m1",
                                  client=client, cost_per_1k_tokens=0.01)
            spec = ToolSpec(name="echo", description="回显", danger=DangerLevel.SAFE)
            req = LLMRequest(messages=[Message("user", "hi")], tools_catalog=[spec],
                             temperature=0.3, max_tokens=64)
            resp = await llm.complete(req, PortCtx("s"))
            chunks = [c async for c in llm.stream(req, PortCtx("s"))]
            return resp, chunks

    resp, chunks = run(scenario())
    assert resp.tool_calls[0].name == "echo" and resp.tool_calls[0].args == {"text": "hi"}
    assert resp.usage == Usage(10, 5, 0.00015)
    assert seen["auth"] == "Bearer KEY" and seen["body"]["tools"][0]["function"]["name"] == "echo"
    assert seen["body"]["temperature"] == 0.3 and seen["body"]["max_tokens"] == 64
    assert chunks == []  # 带 tool_calls 时 content=None


def test_t04_openai_compat_error_is_folded_as_retryable():
    def handler(request):
        return httpx.Response(503, json={"error": "down"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reg = PortRegistry().register(
                "llm", OpenAICompatLLM(base_url="http://fake/v1", client=client)
            )
            return await call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg)

    out = run(scenario())
    assert not out.ok and out.error.retryable and "HTTPStatusError" in out.error.code


def test_t04_in_memory_tool_registry():
    tools = InMemoryToolRegistry()
    spec = ToolSpec(name="add", danger=DangerLevel.SAFE)
    tools.register(spec, lambda args, c: args["a"] + args["b"])
    assert tools.catalog() == [spec]
    assert run(tools.call(ToolCall(call_id="c", name="add", args={"a": 1, "b": 2}), PortCtx("s"))).value == 3
    unknown = run(tools.call(ToolCall(call_id="c", name="nope", args={}), PortCtx("s")))
    assert not unknown.ok and "unknown tool" in unknown.error
    with pytest.raises(ValueError, match="already registered"):
        tools.register(spec, lambda a, c: None)

    async def async_tool(args, c):
        return "async-ok"

    async def broken(args, c):
        raise RuntimeError("tool failed")

    tools.register(ToolSpec(name="a", danger=DangerLevel.SAFE), async_tool)
    tools.register(ToolSpec(name="b", danger=DangerLevel.SAFE), broken)
    assert run(tools.call(ToolCall(call_id="c", name="a", args={}), PortCtx("s"))).value == "async-ok"
    assert not run(tools.call(ToolCall(call_id="c", name="b", args={}), PortCtx("s"))).ok


def test_t04_jsonl_and_null_telemetry(tmp_path: Path):
    tele = JsonlTelemetry(tmp_path / "nested" / "trace.jsonl")
    with tele.span("dispatch", PortCtx("s1")) as span:
        assert span.name == "dispatch" and span.session_id == "s1"
    tele.metric("latency", 12.5, {"kind": "llm"})
    tele.log({"event": "custom"})
    events = tele.read()
    assert [e["event"] for e in events] == ["span_start", "span_end", "metric", "custom"]
    assert events[2]["tags"] == {"kind": "llm"}
    null = NullTelemetry()
    null.metric("x", 1)
    null.log({"a": 1})
    with null.span("s", PortCtx("s")) as span:
        assert span.name == "s"


def test_t04_console_human_and_fakes(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt: "approve")
    assert run(ConsoleHuman().ask("允许?", ["approve", "deny"], PortCtx("s"))) == "approve"

    human = StaticHuman(["yes", "no"])
    assert run(human.ask("q1", None, PortCtx("s"))) == "yes"
    assert run(human.ask("q2", None, PortCtx("s"))) == "no"

    fake = InMemoryTelemetry()
    fake.metric("m", 1)
    with fake.span("sp", PortCtx("s")):
        pass
    assert fake.events[0]["event"] == "metric"

    skills = StaticSkillPort()
    assert "Python" in run(skills.body("python-expert", "body", PortCtx("s")))
    assert run(skills.body("missing", "body", PortCtx("s"))) == ""
    with pytest.raises(TimeoutError):
        run(FailingAdapter().call({}, PortCtx("s")))
    scripted = ScriptedLLM()
    assert len(scripted.requests) == 0


def test_t05_replace_llm_adapter_without_changing_upper_layer():
    from ports import LLMResponse
    reg = PortRegistry().register("llm", ScriptedLLM([LLMResponse(content="first")]))
    first = run(call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg))
    reg.register("llm", ScriptedLLM([LLMResponse(content="second")]))
    second = run(call(Effect(EffectKind.LLM, {"messages": []}), ctx(), reg))
    assert (first.data.content, second.data.content) == ("first", "second")
