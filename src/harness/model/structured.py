"""R4 结构化输出：最小 JSON Schema 校验 + 修复重试提示。

不引入 jsonschema 外部依赖：支持常见关键字（type/required/properties/enum/minimum），
覆盖设计文档的 output 校验场景；复杂 schema 可注入自定义 validator（R13 可插拔）。
红线：strict 模式下未过 schema 的输出绝不标记为合法（不进工具调用/业务逻辑）。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from enum import Enum
from typing import Protocol, runtime_checkable

from runtime import DynamicValue

from .types import StructuredOutputSpec

__all__ = [
    "MinimalSchemaValidator",
    "SchemaValidator",
    "StructuredOutputValidator",
    "validate_json_schema",
]


class _JsonType(str, Enum):
    """JSON Schema 的 type 有限取值（规则 5；私有枚举，不进 G5 公共契约）。

    成员对应 JSON Schema 标准类型；_type_ok 用查表分派，未知值显式回退"不校验"。
    """

    OBJECT = "object"
    ARRAY = "array"
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    NULL = "null"

    @classmethod
    def coerce(cls, raw: object) -> _JsonType | None:
        """字符串归一为成员；未知/缺省值回退 None（不校验，规则 5 显式回退）。"""
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw)
        except ValueError:
            return None


class _OutputMode(str, Enum):
    """结构化输出模式的有限取值（规则 5；与 pydantic Literal 契约对齐）。

    成员：CONSTRAINED 约束解码 / PROMPT 仅提示 / REPAIR 校验失败后修复重试。
    """

    CONSTRAINED = "constrained"
    PROMPT = "prompt"
    REPAIR = "repair"

    @classmethod
    def coerce(cls, raw: str) -> _OutputMode:
        """字符串归一为成员；未知值显式回退 REPAIR（保守校验，规则 5）。"""
        try:
            return cls(raw)
        except ValueError:
            return cls.REPAIR


@runtime_checkable
class SchemaValidator(Protocol):
    """结构化校验器协议（可插拔；可插拔四要素①）。

    成员：
        name: 具名标识，装配期履约校验非空、进 trace。
        validate: 收原始值（str 先试 json.loads）与 schema，返回错误列表；空表示合法。

    复杂 schema（anyOf/pattern/items 等）由注入方基于 jsonschema 实现，框架不内置大依赖。
    """

    name: str

    def validate(self, value: object, schema: dict[str, DynamicValue]) -> list[str]:
        """返回错误描述列表；空列表表示合法。"""
        ...


class MinimalSchemaValidator:
    """默认校验器（可插拔四要素②）：委托模块级 validate_json_schema。

    保持零外部依赖（不引入 jsonschema），覆盖设计文档的 type/required/properties/enum/minimum
    最小校验场景；用户需要更强 schema 时注入自定义 SchemaValidator 替换。
    """

    name: str = "minimal-json-schema"

    def validate(self, value: object, schema: dict[str, DynamicValue]) -> list[str]:
        """调用最小 JSON Schema 校验，返回错误列表。"""
        return validate_json_schema(value, schema)


def validate_json_schema(value: object, schema: dict[str, DynamicValue]) -> list[str]:
    """校验一个值是否符合最小 JSON Schema，返回错误列表（空表示合法）。

    参数：
        value: 待校验值；若为 str 先尝试 json.loads（模型常返回 JSON 文本）。
        schema: 支持 type/required/properties/enum/minimum 关键字。
            schema 由外部注入、形态由 JSON Schema 决定，故用 DynamicValue（规则 3）。
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


def _check(data: DynamicValue, schema: dict[str, DynamicValue], path: str, errors: list[str]) -> None:
    """递归校验单个节点，把错误追加到 errors。

    参数：
        data: 待校验节点（形态由 JSON 决定，DynamicValue 动态边界）。
        schema: 该节点对应的 schema 片段。path: 错误路径（如 $.name）。
        errors: 累计错误列表（原地追加）。
    """
    raw_type = schema.get("type")
    expected = _JsonType.coerce(raw_type)
    if "type" in schema and expected is None:
        # N4：schema 声明了 type 但值非法（如 "str"）→ 报校验错误，不静默跳过。
        # 静默跳过会让坏 schema 下的非法输出被当成合法，与 strict 红线冲突。
        errors.append(f"{path}: invalid schema type {raw_type!r}")
        return
    if expected is not None and not _type_ok(data, expected):
        errors.append(f"{path}: expected {expected.value}, got {type(data).__name__}")
        return  # 类型错则不继续深挖，避免噪音

    raw_enum = schema.get("enum")
    if isinstance(raw_enum, list) and data not in raw_enum:  # 仅 enum 为合法列表时约束取值；坏 schema 形态不误伤合法值
        errors.append(f"{path}: value {data!r} not in enum {raw_enum}")

    if expected is _JsonType.OBJECT and isinstance(data, dict):  # 仅对象类型且数据确为 dict 才深挖字段，避免对标量误报
        for req in schema.get("required", []):  # 逐个必需字段查存在性；不提前 break，一次报全缺失项便于修复
            if req not in data:  # 缺字段即记错；required 列表形态由 schema 保证，异常形态当作空约束
                errors.append(f"{path}.{req}: required field missing")
        raw_props = schema.get("properties", {})
        if isinstance(raw_props, dict):  # properties 形态合法才递归，防止把非 dict 值当成子 schema
            for prop, sub_schema in raw_props.items():  # 逐字段下钻；只校验数据中实际存在的键，缺席交由 required 负责
                if prop in data and isinstance(sub_schema, dict):  # 双重守卫：字段缺席或子 schema 畸形都跳过，兼容宽松 schema
                    _check(data[prop], sub_schema, f"{path}.{prop}", errors)

    if expected in (_JsonType.NUMBER, _JsonType.INTEGER):  # 仅数值类型才谈最小值，字符串比大小无意义
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and isinstance(data, (int, float)) and data < minimum:  # 边界/数据都须是数值；形态不合法视为无约束（显式回退）
            errors.append(f"{path}: {data} < minimum {minimum}")


def _type_ok(data: DynamicValue, expected: _JsonType) -> bool:
    """判断数据是否符合 JSON Schema 的 type 声明（枚举分派，规则 5）。"""
    checkers: dict[_JsonType, Callable[[DynamicValue], bool]] = {
        _JsonType.OBJECT: lambda v: isinstance(v, dict),
        _JsonType.ARRAY: lambda v: isinstance(v, list),
        _JsonType.STRING: lambda v: isinstance(v, str),
        _JsonType.NUMBER: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        _JsonType.INTEGER: lambda v: isinstance(v, int) and not isinstance(v, bool),
        _JsonType.BOOLEAN: lambda v: isinstance(v, bool),
        _JsonType.NULL: lambda v: v is None,
    }
    return checkers[expected](data)


class StructuredOutputValidator:
    """结构化输出校验器：validate() 判定合法性，build_repair_prompt() 生成修复提示。

    属性：
        should_repair: 是否允许修复重试（repair 模式为真；constrained/prompt 为假）。
    """

    def __init__(
        self, spec: StructuredOutputSpec, *, validator: SchemaValidator | None = None
    ) -> None:
        """装配规格 + 校验器（可插拔四要素③替换入口）。

        参数：
            spec: 结构化输出规格。
            validator: 注入的校验器；None 用 MinimalSchemaValidator（四要素②默认）。
        异常：
            TypeError: validator 不满足 SchemaValidator 协议。
            ValueError: validator.name 为空（可插拔物须带 name）。
        """
        self._spec = spec
        self._mode = _OutputMode.coerce(spec.mode)
        chosen = validator if validator is not None else MinimalSchemaValidator()
        # 履约校验（四要素④）：协议成员齐备 + 具名非空，不满足显式拒绝。
        if not isinstance(chosen, SchemaValidator):
            raise TypeError("validator must implement SchemaValidator protocol")
        if not chosen.name:
            raise ValueError("validator.name must be non-empty")
        self._validator = chosen

    @property
    def schema_validator(self) -> SchemaValidator:
        """当前生效的校验器（默认或注入），供外部读取。"""
        return self._validator

    @property
    def should_repair(self) -> bool:
        """是否允许自然语言修复重试（枚举比较，非裸字符串）。"""
        return self._mode is _OutputMode.REPAIR

    @property
    def max_repair(self) -> int:
        """最大修复次数。"""
        return self._spec.max_repair

    def validate(self, text: str) -> tuple[dict[str, DynamicValue] | list[DynamicValue] | str | int | None, bool]:
        """校验模型输出：解析 + schema 校验。

        参数：
            text: 模型原始输出文本。
        返回：
            (解析值, 是否合法)；解析值形态由模型/JSON 决定，故用 DynamicValue（规则 3）。
            strict 模式下非法输出 ok 必为 False。
        """
        # 走注入校验器（默认 MinimalSchemaValidator），判定口径可替换。
        errors = self._validator.validate(text, self._spec.json_schema)
        if errors:
            return None, False
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            # schema 无 type 约束且文本非 JSON：按纯文本成功（合法但无法结构化解析）。
            return text, True
        return parsed, True

    def check(self, text: str) -> list[str]:
        """用当前校验器校验输出文本，返回错误列表（供网关修复循环共用同一口径）。"""
        return self._validator.validate(text, self._spec.json_schema)

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
