"""RED：schema 无 type 约束且文本非 JSON 时按纯文本合法返回 (text, True)。

独立审计 EXP6-D 证据：validate("plain text") 实际返回 (None, False)，
docstring 承诺的纯文本成功分支不可达（validate_json_schema 先报 invalid JSON text）。
"""
from __future__ import annotations

from harness.model import StructuredOutputSpec, StructuredOutputValidator


def test_red_plain_text_passes_when_schema_has_no_type() -> None:
    validator = StructuredOutputValidator(StructuredOutputSpec(schema={}))
    assert validator.validate("plain text") == ("plain text", True)
    assert validator.check("plain text") == [], "check 必须与 validate 同口径"


def test_red_typed_schema_still_rejects_plain_text() -> None:
    """反证：有 type 约束的 schema 不得因文本回退而放松 fail-closed。"""
    validator = StructuredOutputValidator(StructuredOutputSpec(schema={"type": "object"}))
    assert validator.validate("plain text") == (None, False)
    assert validator.check("plain text") != []


def test_red_no_type_schema_still_enforces_enum() -> None:
    """无 type 约束不等于无约束：enum 等关键字仍须生效。"""
    validator = StructuredOutputValidator(
        StructuredOutputSpec(schema={"enum": ["red", "green"]})
    )
    assert validator.validate("red") == ("red", True)
    assert validator.validate("blue") == (None, False)
