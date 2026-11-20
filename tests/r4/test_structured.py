"""R4-T1 结构化输出校验与修复重试：非法输出不进业务 / 超次拒答 / constrained 模式。"""
from __future__ import annotations

from hooks import LLMRequest, LLMResponse

from harness.model import (
    MinimalSchemaValidator,
    SchemaValidator,
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

    def test_bad_schema_type_does_not_skip_validation(self) -> None:
        """N4：schema 的 type 写错（如 "str"）不得静默跳过校验放行非法输出。"""
        bad_schema = {"type": "object", "properties": {"age": {"type": "str"}}}
        errors = validate_json_schema({"age": 123}, bad_schema)
        assert errors, "坏 schema type 应报校验错误，不能静默放行"


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


class TestSchemaValidatorInjection:
    """R4 结构化校验器可插拔（四要素）：协议 + 默认 + 替换入口 + 履约校验。"""

    def test_default_is_minimal_validator(self) -> None:
        """缺省用 MinimalSchemaValidator，行为与旧版一致。"""
        v = StructuredOutputValidator(StructuredOutputSpec(schema=sample_schema()))
        assert isinstance(v.schema_validator, MinimalSchemaValidator)
        assert isinstance(v.schema_validator, SchemaValidator)
        _parsed, ok = v.validate('{"name": "Alice", "age": 30}')
        assert ok is True

    def test_injected_validator_is_used(self) -> None:
        """注入自定义校验器：validate() 必须走它，而非内置最小校验。"""
        class StrictValidator:
            name = "strict-any"
            calls = 0

            def validate(self, value: object, schema: dict) -> list[str]:
                type(self).calls += 1
                return ["custom rejection"]

        v = StructuredOutputValidator(
            StructuredOutputSpec(schema=sample_schema()), validator=StrictValidator()
        )
        parsed, ok = v.validate('{"name": "Alice", "age": 30}')
        assert ok is False and parsed is None
        assert StrictValidator.calls == 1

    def test_injected_validator_can_relax(self) -> None:
        """自定义校验器可返回空错误列表放行内置规则会拒的输出（判定口径由注入方定）。"""
        class LooseValidator:
            name = "loose"

            def validate(self, value: object, schema: dict) -> list[str]:
                return []

        v = StructuredOutputValidator(
            StructuredOutputSpec(schema=sample_schema()), validator=LooseValidator()
        )
        parsed, ok = v.validate('{"age": -1}')  # 内置会拒（缺 name + age<0）
        assert ok is True and parsed is not None  # 注入方放行

    def test_contract_check_rejects_non_validator(self) -> None:
        """履约校验：非 SchemaValidator 实现对注入入口显式拒绝。"""
        import pytest

        with pytest.raises(TypeError, match="SchemaValidator"):
            StructuredOutputValidator(
                StructuredOutputSpec(schema=sample_schema()), validator=object()  # type: ignore[arg-type]
            )

    def test_contract_check_rejects_empty_name(self) -> None:
        """履约校验：name 为空的校验器被拒绝（一切可插拔物带 name，规则）。"""
        import pytest

        class NoNameValidator:
            name = ""

            def validate(self, value: object, schema: dict) -> list[str]:
                return []

        with pytest.raises(ValueError, match="name"):
            StructuredOutputValidator(
                StructuredOutputSpec(schema=sample_schema()), validator=NoNameValidator()
            )
