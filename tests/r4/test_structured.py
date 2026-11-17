"""R4-T1 结构化输出校验与修复重试：非法输出不进业务 / 超次拒答 / constrained 模式。"""
from __future__ import annotations

from hooks import LLMRequest, LLMResponse

from harness.model import (
    StructuredOutputSpec,
    StructuredOutputValidator,
    validate_json_schema,
)
from tests.r4.conftest import resp, sample_schema


class TestValidateJsonSchema:
    def test_valid_object(self) -> None:
        errors = validate_json_schema({"name": "Alice", "age": 30}, sample_schema())
        assert errors == []

    def test_missing_required(self) -> None:
        errors = validate_json_schema({"age": 30}, sample_schema())
        assert any("name" in e for e in errors)

    def test_wrong_type(self) -> None:
        errors = validate_json_schema({"name": 123}, sample_schema())
        assert any("string" in e or "type" in e.lower() for e in errors)

    def test_constraint_violation(self) -> None:
        errors = validate_json_schema({"name": "A", "age": -1}, sample_schema())
        assert any("minimum" in e or ">=" in e for e in errors)

    def test_invalid_json_text(self) -> None:
        """非 JSON 文本：解析失败报错。"""
        errors = validate_json_schema("not json at all", sample_schema())
        assert errors


class TestStructuredOutputValidator:
    def test_valid_output_passes(self) -> None:
        v = StructuredOutputValidator(StructuredOutputSpec(schema=sample_schema()))
        r = resp('{"name": "Alice", "age": 30}')
        parsed, ok = v.validate(r.content or "")
        assert ok is True and parsed["name"] == "Alice"

    def test_repair_prompt_contains_errors(self) -> None:
        """repair 模式：修复提示带上校验错误，让模型纠偏。"""
        v = StructuredOutputValidator(StructuredOutputSpec(schema=sample_schema(), mode="repair"))
        repair = v.build_repair_prompt('{"age": -1}', ["name required", "age >= 0"])
        assert "name required" in repair and "age >= 0" in repair

    def test_constrained_mode_no_repair(self) -> None:
        """constrained 模式：不做自然语言修复（依赖模型侧约束解码）。"""
        v = StructuredOutputValidator(StructuredOutputSpec(schema=sample_schema(), mode="constrained"))
        assert v.should_repair is False

    def test_repair_mode_does_repair(self) -> None:
        v = StructuredOutputValidator(StructuredOutputSpec(schema=sample_schema(), mode="repair"))
        assert v.should_repair is True

    def test_invalid_output_never_returns_as_valid(self) -> None:
        """红线：未过 schema 的输出绝不被标记为 valid（不进业务逻辑）。"""
        v = StructuredOutputValidator(StructuredOutputSpec(schema=sample_schema(), strict=True))
        _parsed, ok = v.validate('{"age": -1}')
        assert ok is False
