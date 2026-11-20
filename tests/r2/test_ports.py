"""R2-T06：全部 Port 协议可解析、DTO 校验通过。"""
import pytest

import inspect

from compose import (
    PORT_PROTOCOLS, LLMPort, ToolPort, MemoryPort, StorePort, TelemetryPort,
    AuditStore, TelemetryExporter,
    LLMRequest, LLMResponse, ToolCall, ToolResult, ToolSpec, Usage, PortCtx, make_call,
)


def test_port_protocols_complete():
    expected = {"llm", "tool", "skill", "rag", "memory", "store", "sandbox",
                "audit", "eval", "telemetry", "human", "mcp", "event_stream", "disclosure"}
    assert expected <= set(PORT_PROTOCOLS)
    for name, proto in PORT_PROTOCOLS.items():
        assert isinstance(proto, type), name


def test_dto_validation():
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}], model="x")
    assert req.messages[0]["content"] == "hi"
    resp = LLMResponse(content="ok", usage=Usage(prompt_tokens=2))
    assert resp.usage.prompt_tokens == 2
    call = make_call("transfer", {"amount": 10}, "c1")
    assert call.name == "transfer" and call.call_id == "c1"
    spec = ToolSpec(name="t", danger="SAFE")
    assert spec.source == "local"


def test_dto_rejects_extra():
    with pytest.raises(Exception):
        ToolCall(call_id="1", name="t", args={}, unexpected="x")


class TestPortAsyncContracts:
    """涉及外部 IO 的 Port 方法必须是 async（防阻塞事件循环回归）。"""

    def test_storage_and_audit_methods_are_async(self) -> None:
        for proto, methods in [
            (StorePort, ("save", "load")),
            (AuditStore, ("append",)),
        ]:
            for m in methods:
                assert inspect.iscoroutinefunction(getattr(proto, m)), f"{proto.__name__}.{m} 应 async"

    def test_telemetry_exporter_async_but_port_sync(self) -> None:
        """遥测：采集 API 同步（OTel 惯例），导出后端 async。"""
        assert inspect.iscoroutinefunction(TelemetryExporter.export)
        assert not inspect.iscoroutinefunction(TelemetryPort.metric)  # 同步采集是有意为之

    def test_new_backends_have_names(self) -> None:
        """新协议带 name 注解（可插拔物强制留名）。"""
        for proto in (AuditStore, TelemetryExporter):
            assert "name" in getattr(proto, "__annotations__", {})
