"""真实链路 e2e：用户自定义 SDK 装填 → OTLP 上报 → 真 Jaeger 容器查询。

纪律：
- 用户侧实现全在 impls/（Podman API / OTLP telemetry / 自定义 telemetry），src 零改；
- TRACE-02 用真 all-in-one 容器（Podman 命名管道 API 起停），禁止进程内 HTTP
  接收端冒充；容器起不来 → pytest.skip 并写明真实错误（环境阻断证据）；
- 查不到 trace / trace_id 对不上 → 真红，不改断言造绿。

反证变异点：
- TRACE-01 ↔ impls/custom_telemetry.CustomTelemetry._register（改坏则不登记/不计数 → 红）；
- TRACE-01 ↔ Agent.trace 从 ObservabilityCapability 取 telemetry 的接线（改回硬编码
  NoopTelemetry → 自定义 SDK 收不到 span → 红）；
- TRACE-02 ↔ OTLPTelemetry._replace 的 _export_span（删则 Jaeger 查不到 trace → 红）；
- TRACE-02 ↔ Agent._derive_call 的 llm/tool 自动埋点（删则无 span → 红）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Generator
from typing import Any

import pytest

from api import Agent
from archs.base import StaticCheckReport
from compose import current_input
from harness.model import ModelCapability
from harness.observability import TraceSpan
from harness.observability.capability import ObservabilityCapability
from runtime import Invoke, Terminate

from conftest import run
from impls.custom_telemetry import CustomTelemetry
from impls.otlp_telemetry import OTLPTelemetry, _hex_id
from impls.recording_llm import RecordingLLM
from impls.podman_api import JaegerContainer, PodmanAPI, free_ports


class TraceArch:
    """最小 Arch：llm Invoke →（可选）tool Invoke → Terminate。"""

    name = "e2e_trace_arch"
    version = "1.0.0"

    def __init__(self, *, with_tool: bool = False) -> None:
        self.with_tool = with_tool

    def static_check(self) -> StaticCheckReport:
        ports = ("llm", "tool") if self.with_tool else ("llm",)
        return StaticCheckReport(errors=(), arch=self.name, version=self.version,
                                 ports_required=ports, budgets_resolved={})

    def compile(self) -> object:
        with_tool = self.with_tool

        def program():
            text = current_input()
            out = yield Invoke(port="llm", payload={
                "layer": "model",
                "messages": [{"role": "user", "content": text}],
            })
            if with_tool:
                yield Invoke(port="tool", payload={
                    "name": "echo", "args": {"text": text},
                    "call_id": "e2e-trace-tool", "danger": "SAFE",
                })
            yield Terminate(output=out)
            return out

        return program()


def _echo(text: str) -> str:
    return f"echo:{text}"


def _wait_jaeger_ready(api_port: int, attempts: int = 40) -> None:
    """轮询 /api/services 直到 200；超时抛带最后错误的 RuntimeError。"""
    url = f"http://127.0.0.1:{api_port}/api/services"
    last = "未发起请求"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
                last = f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 - 环境阻断证据，原样记录
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise RuntimeError(f"Jaeger /api/services {attempts}s 内未就绪，最后错误: {last}")


def _query_traces(api_base: str, service: str,
                  attempts: int = 20) -> tuple[list[dict[str, Any]], str]:
    """轮询 Jaeger v1 /api/traces；返回 (trace 列表, 最后状态/错误)。"""
    url = f"{api_base}/api/traces?service={urllib.parse.quote(service)}"
    last = "未发起请求"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                traces = payload.get("data") or []
                if traces:
                    return list(traces), ""
                last = f"HTTP {resp.status} data=[] total={payload.get('total')}"
        except Exception as exc:  # noqa: BLE001 - 查询失败证据，不在辅助层吞
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    return [], last


@pytest.fixture(scope="function")
def jaeger_container() -> Generator[JaegerContainer, None, None]:
    """用 Podman API 起真 Jaeger all-in-one；teardown 必删容器。"""
    api_port, otlp_port = free_ports(2)
    name = f"e2e-jaeger-{uuid.uuid4().hex[:12]}"
    api = PodmanAPI()
    container_id = ""
    try:
        container_id = api.create_jaeger(name, api_port, otlp_port)
        api.start(container_id)
        _wait_jaeger_ready(api_port)
    except Exception as exc:  # noqa: BLE001 - 环境阻断如实登记，不用桩顶替
        if container_id:
            api.cleanup(container_id)
        pytest.skip(
            "真实 Jaeger 容器不可用（环境阻断证据，未用进程内接收端冒充）: "
            f"{type(exc).__name__}: {exc}"
        )
    info = JaegerContainer(container_id, name, api_port, otlp_port)
    try:
        yield info
    finally:
        api.cleanup(info.container_id)


def test_trace01_custom_sdk_installed(loop: asyncio.AbstractEventLoop) -> None:
    """TRACE-01 用户自定义 telemetry 必须被门面真装填。

    真跑一轮 Agent.asay：spans() 收到 llm span、所有 span 的 trace_id 与门面
    trace_id 一致、_register 装填计数 > 0。
    """
    async def scenario() -> None:
        custom = CustomTelemetry()
        llm = RecordingLLM(answer="TRACE-01-OK")
        agent = Agent(TraceArch(), session_id="e2e-trace01").use(
            ModelCapability(llm=llm)
        ).use(ObservabilityCapability(telemetry=custom))

        answer = await agent.asay("报数")
        assert answer == "TRACE-01-OK"
        assert llm.complete_calls >= 1

        trace_id = agent.trace.trace_id
        spans = custom.spans()
        assert any(s.name == "llm" for s in spans), f"自定义 SDK 未收到 llm span: {spans!r}"
        assert spans and all(s.trace_id == trace_id for s in spans), (
            f"span trace_id 须一致为 {trace_id!r}: {spans!r}")
        assert custom.register_calls > 0, "_register 未被真调用（装填断链）"
        assert custom.replace_calls > 0, "_replace 未被真调用（span 未结束）"
    run(scenario(), loop)


def test_trace02_real_jaeger_otlp_e2e(
    loop: asyncio.AbstractEventLoop, jaeger_container: JaegerContainer
) -> None:
    """TRACE-02 真 Jaeger 容器 e2e：OTLP 上报 + Jaeger API 反查。

    带工具跑一轮 Agent.asay → llm/tool span 经 OTLP POST 进真容器 → 按
    service 查 /api/traces，断言 trace 数 >= 1、trace_id 等于框架 id 的
    OTLP hex 映射、span 名含 llm 或 tool。
    """
    service_name = f"agentkit-e2e-{uuid.uuid4().hex[:8]}"
    otlp = OTLPTelemetry(endpoint=jaeger_container.otlp_endpoint,
                         service_name=service_name)

    async def scenario() -> None:
        llm = RecordingLLM(answer="TRACE-02-OK")
        agent = Agent(TraceArch(with_tool=True), session_id="e2e-trace02").use(
            ModelCapability(llm=llm)
        ).use(ObservabilityCapability(telemetry=otlp))
        agent.register_tool("echo", _echo)

        answer = await agent.asay("查一下")
        assert answer == "TRACE-02-OK"
        assert llm.complete_calls >= 1

        trace_id = agent.trace.trace_id
        recorded = otlp.spans()
        names = [s.name for s in recorded]
        assert any(n == "llm" for n in names) and any(n.startswith("tool:") for n in names), (
            f"本轮须同时产生 llm/tool span: {names!r}")
        assert all(s.trace_id == trace_id for s in recorded)
        assert otlp.export_successes() >= 1, (
            f"OTLP /v1/traces 未成功上报（failures={otlp.export_failures()}）")

        expected_hex = _hex_id(trace_id, 32)
        traces, last = _query_traces(jaeger_container.api_base, service_name)
        assert len(traces) >= 1, (
            f"Jaeger 未查到 service={service_name} 的 trace（最后状态: {last}）")
        trace_ids = [str(t.get("traceID", "")) for t in traces]
        assert expected_hex in trace_ids, (
            f"Jaeger traceID 须含 OTLP 映射值 {expected_hex!r}，实际={trace_ids!r}")
        operations = [
            str(span.get("operationName", ""))
            for trace in traces for span in (trace.get("spans") or [])
        ]
        assert any("llm" in name or "tool" in name for name in operations), (
            f"Jaeger span 名须含 llm 或 tool: {operations!r}")
        return len(traces)
    trace_count = run(scenario(), loop)
    print(f"TRACE-02 真容器 {jaeger_container.name} 查到 trace 数={trace_count}")


def test_trace03_otlp_body_mapping() -> None:
    """TRACE-03 OTLPTelemetry._otlp_body 对真 TraceSpan 的映射正确。"""
    span = TraceSpan(
        trace_id="e2e-trace-03", span_id="e2e-span-03", name="llm",
        component="llm", duration_ms=12.5, tokens=7, cost=0.0003,
    )
    telemetry = OTLPTelemetry(endpoint="http://127.0.0.1:1",
                              service_name="agentkit-e2e-unit")
    body = telemetry._otlp_body(span)

    resource = body["resourceSpans"][0]
    item = resource["scopeSpans"][0]["spans"][0]
    assert item["traceId"] == _hex_id(span.trace_id, 32)
    assert re.fullmatch(r"[0-9a-f]{32}", item["traceId"]), item["traceId"]
    assert item["spanId"] == _hex_id(span.span_id, 16)
    assert re.fullmatch(r"[0-9a-f]{16}", item["spanId"]), item["spanId"]
    assert item["name"] == span.name
    assert item["status"] == {"code": 0}
    attrs = {a["key"]: a["value"] for a in resource["resource"]["attributes"]}
    assert attrs["service.name"]["stringValue"] == "agentkit-e2e-unit"
