"""R4 结构化输出：最小 JSON Schema 校验 + 修复重试提示。

不引入 jsonschema 外部依赖：支持常见关键字（type/required/properties/enum/minimum），
覆盖设计文档的 output 校验场景；复杂 schema 可注入自定义 validator（R13 可插拔）。
红线：strict 模式下未过 schema 的输出绝不标记为合法（不进工具调用/业务逻辑）。
"""
from __future__ import annotations

import json
from typing import Any

from .types import StructuredOutputSpec

__all__ = ["validate_json_schema", "StructuredOutputValidator"]


def validate_json_schema(value: object, schema: dict[str, Any]) -> list[str]:
    """校验一个值是否符合最小 JSON Schema，返回错误列表（空表示合法）。

    参数：
        value: 待校验值；若为 str 先尝试 json.loads（模型常返回 JSON 文本）。
        schema: 支持 type/required/properties/enum/minimum 关键字。
    返回：
        错误描述列表；空列表表示通过。
    """
    errors: list[str] = []
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            return [f"invalid JSON text: {value[:80]!r}"]
    _check(data, schema, "$", errors)
    return errors


def _check(data: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    """递归校验单个节点，把错误追加到 errors。"""
    expected = schema.get("type")
    if expected:
        ok = _type_ok(data, expected)
        if not ok:
            errors.append(f"{path}: expected {expected}, got {type(data).__name__}")
            return  # 类型错则不继续深挖，避免噪音

    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: value {data!r} not in enum {schema['enum']}")

    if expected == "object" and isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"{path}.{req}: required field missing")
        for prop, sub_schema in schema.get("properties", {}).items():
            if prop in data:
                _check(data[prop], sub_schema, f"{path}.{prop}", errors)

    if expected == "number" or expected == "integer":
        minimum = schema.get("minimum")
        if minimum is not None and isinstance(data, (int, float)) and data < minimum:
            errors.append(f"{path}: {data} < minimum {minimum}")


def _type_ok(data: Any, expected: str) -> bool:
    """判断数据是否符合 JSON Schema 的 type 声明。"""
    mapping = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    check = mapping.get(expected)
    return check(data) if check else True


class StructuredOutputValidator:
    """结构化输出校验器：validate() 判定合法性，build_repair_prompt() 生成修复提示。

    属性：
        should_repair: 是否允许修复重试（repair 模式为真；constrained/prompt 为假）。
    """

    def __init__(self, spec: StructuredOutputSpec) -> None:
        """装配规格。"""
        self._spec = spec

    @property
    def should_repair(self) -> bool:
        """是否允许自然语言修复重试。"""
        return self._spec.mode == "repair"

    @property
    def max_repair(self) -> int:
        """最大修复次数。"""
        return self._spec.max_repair

    def validate(self, text: str) -> tuple[dict[str, Any] | list[Any] | str | int | None, bool]:
        """校验模型输出：解析 + schema 校验。

        参数：
            text: 模型原始输出文本。
        返回：
            (解析值, 是否合法)；strict 模式下非法输出 ok 必为 False。
        """
        errors = validate_json_schema(text, self._spec.json_schema)
        if errors:
            return None, False
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            # schema 无 type 约束且文本非 JSON：按纯文本成功（合法但无法结构化解析）。
            return text, True
        return parsed, True

    def build_repair_prompt(self, bad_output: str, errors: list[str]) -> str:
        """生成修复提示：附上原输出与校验错误，让模型纠偏。"""
        lines = [
            "Your previous output failed schema validation.",
            f"Original output: {bad_output}",
            "Validation errors:",
            *[f"  - {e}" for e in errors],
            "Return a corrected JSON object that satisfies the schema.",
        ]
        return "\n".join(lines)
