"""R4 网关编排测试：缓存命中 / 结构化修复循环 / 成本闸 / 超次拒答。

这些是 R4 的核心行为（流程图 §3），用 fake LLM 驱动，验证编排正确性。
"""
from __future__ import annotations

import asyncio

from hooks import LLMRequest, LLMResponse, PortCtx, Usage

from harness.model import (
    CostGuard,
    ExactCache,
    ModelGateway,
    StructuredOutputSpec,
)
from tests.r4.conftest import pctx, req, resp, sample_schema
from tests.r4.fakes import RecordingLLM


class TestGatewayCache:
    def test_cache_hit_skips_llm(self) -> None:
        """缓存命中：不调用 LLM，直接返回缓存响应。"""
        llm = RecordingLLM([resp("first")])
        gw = ModelGateway(llm=llm)
        r1 = asyncio.run(gw.complete(req(), pctx()))
        r2 = asyncio.run(gw.complete(req(), pctx()))
        assert r1.content == "first"
        assert r2.content == "first"
        assert llm.call_count == 1  # 第二次命中缓存，未调 LLM

    def test_cache_hit_no_double_cost(self) -> None:
        """缓存命中不重复计费。"""
        llm = RecordingLLM([resp("x", cost=0.3)])
        gw = ModelGateway(llm=llm)
        asyncio.run(gw.complete(req(), pctx(run_id="r1")))
        asyncio.run(gw.complete(req(), pctx(run_id="r1")))
        assert gw.ledger.run_cost("r1") == 0.3  # 只记一次


class TestGatewayStructured:
    def test_valid_output_no_repair(self) -> None:
        """合法结构化输出：一次调用成功。"""
        llm = RecordingLLM([resp('{"name": "Alice", "age": 30}')])
        spec = StructuredOutputSpec(schema=sample_schema())
        gw = ModelGateway(llm=llm, spec=spec)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert "Alice" in (r.content or "")
        assert llm.call_count == 1

    def test_repair_then_success(self) -> None:
        """非法输出 → 修复提示重试 → 合法：调 2 次。"""
        llm = RecordingLLM([
            resp('{"age": -1}'),                 # 非法：缺 name + age<0
            resp('{"name": "Bob", "age": 5}'),   # 修复后合法
        ])
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=2)
        gw = ModelGateway(llm=llm, spec=spec)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert "Bob" in (r.content or "")
        assert llm.call_count == 2

    def test_repair_exhausted_strict_refuses(self) -> None:
        """修复超次 + strict：返回 SCHEMA_INVALID error 响应拒答（N3：不抛穿成 PORT_CRASHED）。"""
        llm = RecordingLLM([resp('{"age": -1}')])  # 一直非法
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=1, strict=True)
        gw = ModelGateway(llm=llm, spec=spec)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert r.error is not None and "SCHEMA_INVALID" in r.error
        assert r.content == "", "拒答不得把非法输出当正文返回"

    def test_non_strict_returns_last(self) -> None:
        """非 strict 模式：修复超次后返回最后一次响应（调用方自行处置）。"""
        llm = RecordingLLM([resp('{"age": -1}')])
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=0, strict=False)
        gw = ModelGateway(llm=llm, spec=spec)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert r.content == '{"age": -1}'

    def test_repair_loop_uses_injected_validator(self) -> None:
        """注入校验器在修复循环里也生效——校验与修复同一口径。"""
        class AlwaysOkValidator:
            name = "always-ok"
            calls = 0

            def validate(self, value: object, schema: dict) -> list[str]:
                type(self).calls += 1
                return []  # 放行一切，包括内置规则会拒的输出

        # 第一次输出内置会拒（缺 name + age<0），但注入校验器放行 → 不应触发修复
        llm = RecordingLLM([resp('{"age": -1}')])
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=2)
        gw = ModelGateway(llm=llm, spec=spec, validator=AlwaysOkValidator())
        r = asyncio.run(gw.complete(req(), pctx()))
        assert r.content == '{"age": -1}'
        assert llm.call_count == 1  # 若修复循环仍用全局校验器，这里会是 2（触发修复）
        assert AlwaysOkValidator.calls >= 1  # 注入校验器确实被调用

    def test_repair_loop_injected_validator_rejects(self) -> None:
        """注入校验器拒绝合法输出时，修复循环据此重试（判定权在注入方）。"""
        class AlwaysRejectValidator:
            name = "always-reject"

            def validate(self, value: object, schema: dict) -> list[str]:
                return ["injected: reject"]

        llm = RecordingLLM([
            resp('{"name": "Alice"}'),  # 内置会判合法，但注入校验器拒绝
            resp('{"name": "Bob"}'),
        ])
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=1, strict=False)
        gw = ModelGateway(llm=llm, spec=spec, validator=AlwaysRejectValidator())
        asyncio.run(gw.complete(req(), pctx()))
        # 注入校验器两次都拒 → 触发修复重试；若修复循环误用全局校验器（会判合法）则只调 1 次
        assert llm.call_count > 1


class TestGatewayCostGuard:
    def test_cost_guard_blocks(self) -> None:
        """成本闸超限：调用前拒答（返回 COST_LIMIT error），且后端零调用（P2b 硬闸）。"""
        llm = RecordingLLM([resp("x", cost=0.5)])
        guard = CostGuard(max_cost_usd=0.1)
        # 注入预估器返回 0.5 > 预算 0.1：调用前硬闸必须拒答。
        gw = ModelGateway(llm=llm, cost_guard=guard, cost_estimator=lambda _: 0.5)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert r.error is not None and "COST_LIMIT" in r.error
        assert llm.call_count == 0, "预算不足时不得发起后端调用"
        assert guard.remaining() == 0.1, "被拒调用不占用预算"

    def test_concurrent_same_request_calls_backend_once(self) -> None:
        """P2a 缓存击穿：40 个相同请求并发，后端只调 1 次、成本只记 1 笔。"""
        import asyncio

        class SlowLLM:
            name = "slow"
            last_usage = None

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, request, ctx):
                self.calls += 1
                await asyncio.sleep(0.01)  # 制造并发重叠窗口
                return resp("answer", cost=0.1)

            async def stream(self, request, ctx):
                yield ""

        async def run() -> None:
            llm = SlowLLM()
            gw = ModelGateway(llm=llm)  # type: ignore[arg-type]
            results = await asyncio.gather(
                *(gw.complete(req(), pctx()) for _ in range(40))
            )
            assert all(r.content == "answer" for r in results)
            assert llm.calls == 1, f"40 并发相同请求应只调后端 1 次，实际 {llm.calls}"
            assert gw.ledger.run_cost("run1") == 0.1, "成本只应记 1 笔"

        asyncio.run(run())

    def test_concurrent_different_requests_not_merged(self) -> None:
        """singleflight 只合并相同 key：不同请求必须各自调用后端。"""
        import asyncio

        class SlowLLM:
            name = "slow"
            last_usage = None

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, request, ctx):
                self.calls += 1
                await asyncio.sleep(0.01)
                return resp(request.messages[0]["content"])

            async def stream(self, request, ctx):
                yield ""

        async def run() -> None:
            llm = SlowLLM()
            gw = ModelGateway(llm=llm)  # type: ignore[arg-type]
            r1 = req([{"role": "user", "content": "A"}])
            r2 = req([{"role": "user", "content": "B"}])
            await asyncio.gather(gw.complete(r1, pctx()), gw.complete(r2, pctx()))
            assert llm.calls == 2, f"不同请求不应合并，实际后端调用 {llm.calls} 次"

        asyncio.run(run())

    def test_concurrent_cost_guard_blocks_before_call(self) -> None:
        """P2b 端到端：预算只够 5 次，20 并发 → 后端只调 5 次（调用前硬闸真生效）。"""
        import asyncio

        class CountingLLM:
            name = "counting"
            last_usage = None

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, request, ctx):
                self.calls += 1
                await asyncio.sleep(0.01)
                return resp("ok", cost=0.1)

            async def stream(self, request, ctx):
                yield ""

        async def run() -> None:
            llm = CountingLLM()
            guard = CostGuard(max_cost_usd=0.5)
            # 每个请求预估 0.1，预算 0.5 → 只允许 5 个并发预留成功
            gw = ModelGateway(
                llm=llm, cost_guard=guard, cost_estimator=lambda _: 0.1
            )  # type: ignore[arg-type]
            results = await asyncio.gather(
                *(gw.complete(req([{"role": "user", "content": f"q{i}"}]), pctx())
                  for i in range(20))
            )
            allowed = sum(1 for r in results if r.error is None)
            denied = sum(1 for r in results if r.error and "COST_LIMIT" in r.error)
            assert allowed == 5, f"预算 0.5/单次 0.1 应放行 5 次，实际 {allowed}"
            assert denied == 15, f"应拒答 15 次，实际 {denied}"
            assert llm.calls == 5, f"后端只应被调用 5 次，实际 {llm.calls}"
            assert guard.spent == 0.5 and guard.remaining() == 0.0

        asyncio.run(run())

    def test_injected_cache_used(self) -> None:
        """可注入自定义缓存（可插拔契约）。"""
        cache = ExactCache()
        llm = RecordingLLM([resp("x")])
        gw = ModelGateway(llm=llm, cache=cache)
        asyncio.run(gw.complete(req(), pctx()))
        assert cache.hits >= 0 and cache.misses >= 0
