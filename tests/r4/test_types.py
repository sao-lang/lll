"""R4-T1 核心类型测试：StructuredOutputSpec / 缓存键 / 成本归因维度 / 路由协议。"""
from __future__ import annotations

from hooks import LLMRequest, LLMResponse, Usage

from harness.model import (
    CacheKey,
    CostRecord,
    ModelRouter,
    CachePort,
    StructuredOutputSpec,
    UsageLedger,
    build_cache_key,
)


class TestStructuredOutputSpec:
    def test_defaults(self) -> None:
        spec = StructuredOutputSpec(schema={"type": "object"})
        assert spec.mode == "repair"
        assert spec.max_repair == 2
        assert spec.strict is True

    def test_mode_literal_validation(self) -> None:
        """mode 只接受 constrained/prompt/repair。"""
        from pydantic import ValidationError
        try:
            StructuredOutputSpec(schema={}, mode="bogus")  # type: ignore[arg-type]
        except ValidationError:
            pass
        else:
            raise AssertionError("expected ValidationError for bad mode")

    def test_invalid_max_repair_rejected(self) -> None:
        from pydantic import ValidationError
        try:
            StructuredOutputSpec(schema={}, max_repair=-1)
        except ValidationError:
            pass
        else:
            raise AssertionError("expected ValidationError for negative max_repair")


class TestCacheKey:
    def test_same_inputs_same_key(self) -> None:
        r = LLMRequest(messages=[{"role": "user", "content": "hi"}], model="m1", temperature=0.5)
        k1 = build_cache_key(r, prompt_version="v1")
        k2 = build_cache_key(r, prompt_version="v1")
        assert k1 == k2

    def test_version_changes_key(self) -> None:
        """cache key 含 prompt_version：版本变化必须失效。"""
        r = LLMRequest(messages=[{"role": "user", "content": "hi"}], model="m1")
        assert build_cache_key(r, prompt_version="v1") != build_cache_key(r, prompt_version="v2")

    def test_model_and_messages_change_key(self) -> None:
        r1 = LLMRequest(messages=[{"role": "user", "content": "hi"}], model="m1")
        r2 = LLMRequest(messages=[{"role": "user", "content": "hi"}], model="m2")
        r3 = LLMRequest(messages=[{"role": "user", "content": "bye"}], model="m1")
        assert build_cache_key(r1) != build_cache_key(r2)
        assert build_cache_key(r1) != build_cache_key(r3)

    def test_cache_key_is_dataclass(self) -> None:
        ck = CacheKey(value="abc", version="v1")
        assert ck.value == "abc" and ck.version == "v1"


class TestUsageLedger:
    """成本归因：per-run / per-agent / per-user / per-model 多维累计。"""

    def _record(self, run: str, agent: str, user: str, model: str, cost: float, tokens: int) -> CostRecord:
        return CostRecord(
            run_id=run, agent_id=agent, user_id=user, model=model,
            usage=Usage(prompt_tokens=tokens, completion_tokens=0, cost_usd=cost),
        )

    def test_accumulate_by_dimensions(self) -> None:
        ledger = UsageLedger()
        ledger.record(self._record("r1", "a1", "u1", "m1", 0.5, 100))
        ledger.record(self._record("r1", "a1", "u1", "m2", 0.3, 60))
        ledger.record(self._record("r2", "a2", "u2", "m1", 0.2, 40))
        assert ledger.total() == 0.5 + 0.3 + 0.2
        assert ledger.by("run_id", "r1") == 0.8
        assert ledger.by("model", "m1") == 0.7
        assert ledger.by("user_id", "u2") == 0.2
        assert ledger.tokens_by("model", "m1") == 140

    def test_empty_dimension_zero(self) -> None:
        ledger = UsageLedger()
        assert ledger.total() == 0.0
        assert ledger.by("model", "none") == 0.0


class TestProtocols:
    def test_model_router_protocol_shape(self) -> None:
        """ModelRouter 是可注入协议：带 name + route。"""
        assert hasattr(ModelRouter, "route")
        assert hasattr(CachePort, "get")
