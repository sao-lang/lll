"""M3-T02 PortRegistry：命名空间、装配期缺失清单、替换实现。"""
from __future__ import annotations

import pytest

from agentkit.ports import PortContractError, PortRegistry
from agentkit.ports.adapters.fakes import ScriptedLLM
from agentkit.runtime import PortMissing


def test_t02_custom_namespace_rule():
    reg = PortRegistry()
    reg.register("x-vector-db", object())
    with pytest.raises(PortContractError, match="x-"):
        reg.register("vector_db", object())
    assert reg.names() == ("x-vector-db",)


def test_t02_freeze_lists_every_missing_port():
    reg = PortRegistry().register("llm", ScriptedLLM())
    with pytest.raises(PortMissing, match="telemetry, tool"):
        reg.freeze(("llm", "tool", "telemetry"))
    assert reg.missing(("llm",)) == [] and reg.freeze(("llm",)) is reg


def test_t02_resolve_and_try_resolve():
    reg = PortRegistry().register("llm", ScriptedLLM())
    assert reg.resolve("llm") is reg.try_resolve("llm")
    assert reg.try_resolve("missing") is None
    with pytest.raises(PortMissing):
        reg.resolve("missing")


def test_t02_replacement_is_one_line():
    from agentkit.ports import LLMResponse, call
    from agentkit.runtime import Effect, EffectKind
    from tests.fakes import run

    reg = PortRegistry().register("llm", ScriptedLLM([LLMResponse(content="A")]))
    first = run(call(Effect(EffectKind.LLM, {"messages": []}), object(), reg))
    reg.register("llm", ScriptedLLM([LLMResponse(content="B")]))
    second = run(call(Effect(EffectKind.LLM, {"messages": []}), object(), reg))
    assert first.data.content == "A" and second.data.content == "B"
