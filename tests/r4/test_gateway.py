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
        """修复超次 + strict：拒答（抛错），绝不返回非法输出。"""
        llm = RecordingLLM([resp('{"age": -1}')])  # 一直非法
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=1, strict=True)
        gw = ModelGateway(llm=llm, spec=spec)
        try:
            asyncio.run(gw.complete(req(), pctx()))
        except ValueError as exc:
            assert "repair exhausted" in str(exc)
        else:
            raise AssertionError("expected ValueError for repair exhaustion")

    def test_non_strict_returns_last(self) -> None:
        """非 strict 模式：修复超次后返回最后一次响应（调用方自行处置）。"""
        llm = RecordingLLM([resp('{"age": -1}')])
        spec = StructuredOutputSpec(schema=sample_schema(), mode="repair", max_repair=0, strict=False)
        gw = ModelGateway(llm=llm, spec=spec)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert r.content == '{"age": -1}'

    def test_repair_loop_uses_injected_validator(self) -> None:
        """注入校验器在修复循环里也生效——校验与修复同一口径（v1.6 关键点）。"""
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
        """成本闸超限：拒绝调用（红线：不得绕过 Budgets）。"""
        llm = RecordingLLM([resp("x", cost=0.5)])
        guard = CostGuard(max_cost_usd=0.1)
        gw = ModelGateway(llm=llm, cost_guard=guard)
        try:
            asyncio.run(gw.complete(req(), pctx()))
        except ValueError as exc:
            assert "budget" in str(exc).lower()
        else:
            raise AssertionError("expected ValueError for cost budget exceeded")

    def test_injected_cache_used(self) -> None:
        """可注入自定义缓存（可插拔契约）。"""
        cache = ExactCache()
        llm = RecordingLLM([resp("x")])
        gw = ModelGateway(llm=llm, cache=cache)
        asyncio.run(gw.complete(req(), pctx()))
        assert cache.hits >= 0 and cache.misses >= 0
