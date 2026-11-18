"""R4-T5 换后端测试：换 LLMPort 实现上层零改动 + 流式可中断 + 成本从 usage 提取。"""
from __future__ import annotations

import asyncio

from hooks import LLMPort, LLMRequest, LLMResponse, PortCtx, Usage

from harness.model import ModelCapability, ModelGateway
from tests.r4.conftest import pctx, resp
from tests.r4.fakes import RecordingLLM, StreamLLM


class TestRecordingLLM:
    def test_implements_llm_port(self) -> None:
        assert isinstance(RecordingLLM([]), LLMPort)

    def test_complete_returns_queued_response(self) -> None:
        llm = RecordingLLM([resp("a"), resp("b")])
        r1 = asyncio.run(llm.complete(LLMRequest(), pctx()))
        r2 = asyncio.run(llm.complete(LLMRequest(), pctx()))
        assert r1.content == "a" and r2.content == "b"

    def test_calls_recorded(self) -> None:
        llm = RecordingLLM([resp("a")])
        asyncio.run(llm.complete(LLMRequest(), pctx()))
        assert llm.call_count == 1


class TestSwapBackend:
    """换 LLMPort 实现：上层（ModelGateway）零改动。"""

    def test_gateway_works_with_any_llm(self) -> None:
        llm_a = RecordingLLM([resp("from-a")])
        llm_b = RecordingLLM([resp("from-b")])
        gw_a = ModelGateway(llm=llm_a)
        gw_b = ModelGateway(llm=llm_b)
        ra = asyncio.run(gw_a.complete(LLMRequest(), pctx()))
        rb = asyncio.run(gw_b.complete(LLMRequest(), pctx()))
        assert ra.content == "from-a" and rb.content == "from-b"

    def test_gateway_cost_attribution(self) -> None:
        """网关把 usage.cost_usd 计入账本。"""
        llm = RecordingLLM([resp("x", cost=0.25)])
        gw = ModelGateway(llm=llm)
        asyncio.run(gw.complete(LLMRequest(), pctx(run_id="r9")))
        assert gw.ledger.run_cost("r9") == 0.25


class TestStreaming:
    def test_stream_yields_chunks(self) -> None:
        llm = StreamLLM(chunks=["he", "llo"])
        out = asyncio.run(_collect(llm.stream(LLMRequest(), pctx())))
        assert "".join(out) == "hello"

    def test_stream_cancellable(self) -> None:
        """流式取消可中断：消费一个 chunk 后停止，不阻塞。"""
        llm = StreamLLM(chunks=["a", "b", "c"])

        async def consume_one():
            it = llm.stream(LLMRequest(), pctx())
            first = await anext(it)
            await it.aclose()  # 取消
            return first

        assert asyncio.run(consume_one()) == "a"


async def _collect(it) -> list[str]:
    """收集异步迭代器全部元素。"""
    return [item async for item in it]


class TestModelCapability:
    """能力装配：llm/router/cache 注册进 Registry，gateway() 构造网关。"""

    def test_add_backend_registers_degrade_target(self) -> None:
        """v5：降级目标后端通过 add_backend 注册到 ModelRegistry。"""
        cap = ModelCapability(llm=RecordingLLM([resp("x")]))
        backup = RecordingLLM([resp("backup")])
        cap.add_backend("backup", backup)
        gw = cap.gateway()
        out = asyncio.run(gw.complete(
            LLMRequest(), pctx(), degrade_target="backup"
        ))
        assert out.content == "backup"

    def test_gateway_factory(self) -> None:
        cap = ModelCapability(llm=RecordingLLM([resp("x")]))
        gw = cap.gateway()
        assert isinstance(gw, ModelGateway)
        out = asyncio.run(gw.complete(LLMRequest(), pctx()))
        assert out.content == "x"

    def test_gateway_factory_passes_validator(self) -> None:
        """能力装配路径也要透传注入校验器（注入点打通 ModelCapability → ModelGateway）。"""
        from harness.model import StructuredOutputSpec
        from tests.r4.conftest import req, sample_schema

        class AlwaysOkValidator:
            name = "always-ok"

            def validate(self, value: object, schema: dict) -> list[str]:
                return []  # 放行一切；内置校验器会拒（缺 name）

        llm = RecordingLLM([resp('{"age": -1}')])
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=2)
        cap = ModelCapability(llm=llm, spec=spec, validator=AlwaysOkValidator())
        gw = cap.gateway()
        out = asyncio.run(gw.complete(req(), pctx()))
        assert out.content == '{"age": -1}'
        assert gw._validator.schema_validator.name == "always-ok"  # 注入的校验器确实装配上
        assert llm.call_count == 1  # 若未透传（用内置校验器）会触发修复 → 2 次
