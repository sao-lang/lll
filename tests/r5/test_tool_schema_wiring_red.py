"""RED：@tool 支持 description 形参 + 从逐参数类型注解推导 JSON Schema（体检 B8）。"""
from __future__ import annotations

from compose import DangerLevel
from harness.tools import tool


def test_red_tool_accepts_description_and_derives_schema() -> None:
    """目标写法：@tool(description=..., danger=...) + city: str 推导 schema。"""
    @tool(description="查询天气", danger=DangerLevel.LOW)
    def get_weather(city: str, units: int = 1) -> dict:  # type: ignore[no-untyped-def]
        """被 docstring 覆盖的描述"""
        return {"city": city}

    spec = get_weather.spec
    assert spec.description == "查询天气", f"description 形参应生效，实得 {spec.description!r}"
    schema = spec.parameters_schema
    props = schema.get("properties", {})
    assert "city" in props, f"city: str 应推导出 schema 属性，实得 {schema!r}"
    assert props.get("city", {}).get("type") == "string", f"city 应为 string，实得 {props.get('city')!r}"
    assert props.get("units", {}).get("type") == "integer", f"units: int 应为 integer，实得 {props.get('units')!r}"
    assert schema.get("required") == ["city"], f"仅无默认值的 city 应必填，实得 {schema.get('required')!r}"
