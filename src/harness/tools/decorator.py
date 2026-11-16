"""M4 @tool 装饰器：pydantic model 即 schema，docstring 即 description，danger 必填。

支持两种写法：
1. 装饰函数（推荐）：参数模型取自第一个参数注解或 args= 显式指定；
2. 装饰 pydantic BaseModel 类：生成 spec 后由 registry.bind(name, fn) 绑定实现
   （与设计方案 §4 的「@tool 装饰 Args 模型 + 下方定义函数」示意等价）。

默认 extra="forbid" 语义由两点保证：schema 顶层 additionalProperties=False +
registry.call 运行时拒绝未声明字段（无需改动用户的 model_config）。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, get_type_hints

from pydantic import BaseModel

from ports import DangerLevel, ToolSpec

__all__ = ["SandboxPolicy", "ToolDefinition", "ToolDefinitionError", "tool"]


class SandboxPolicy(str, Enum):
    AUTO = "auto"      # LOW 及以上自动沙盒
    ALWAYS = "always"  # 任何工具都沙盒
    NEVER = "never"    # 显式豁免（需审查）


class ToolDefinitionError(Exception):
    """注册期定义错误（重名 / 缺 danger / 非法 schema / 缺沙盒）。"""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    spec: ToolSpec
    args_model: type[BaseModel] | None
    fn: Callable[..., Any] | None
    sandbox: SandboxPolicy = SandboxPolicy.AUTO
    timeout: float = 30.0
    retries: int = 2


def _schema_of(model: type[BaseModel] | None) -> dict[str, Any]:
    if model is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _infer_args(target: Callable[..., Any]) -> type[BaseModel] | None:
    try:
        hints = get_type_hints(target)
    except Exception:  # pragma: no cover - 无注解 / 前向引用未解析
        return None
    params = [p for p in inspect.signature(target).parameters.values()
              if p.name not in ("self", "cls")]
    if not params:
        return None
    hint = hints.get(params[0].name)
    return hint if isinstance(hint, type) and issubclass(hint, BaseModel) else None


def tool(
    *,
    name: str | None = None,
    danger: DangerLevel | str | None = None,
    args: type[BaseModel] | None = None,
    description: str | None = None,
    sandbox: SandboxPolicy = SandboxPolicy.AUTO,
    timeout: float = 30.0,
    retries: int = 2,
    source: Literal["local", "mcp", "skill"] = "local",
) -> Callable[[Any], Any]:
    """把函数 / pydantic 模型声明为工具；danger 缺失直接注册期拒绝（M4 红线）。"""
    if danger is None:
        raise ToolDefinitionError("every @tool must declare danger (SAFE/LOW/HIGH/CRITICAL)")
    level = DangerLevel(danger)

    def wrap(target: Any) -> Any:
        is_model = isinstance(target, type) and issubclass(target, BaseModel)
        if not is_model and not callable(target):
            raise ToolDefinitionError("@tool can decorate a function or a pydantic BaseModel")
        model = target if is_model else (args or _infer_args(target))
        if model is not None and not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise ToolDefinitionError("args must be a pydantic BaseModel subclass")
        tool_name = name or target.__name__
        doc = description or (inspect.getdoc(target) or "").strip().split("\n")[0] or tool_name
        spec = ToolSpec(
            name=tool_name,
            description=doc,
            parameters_schema=_schema_of(model),
            danger=level,
            source=source,
        )
        definition = ToolDefinition(
            name=tool_name,
            spec=spec,
            args_model=model,
            fn=None if is_model else target,
            sandbox=sandbox,
            timeout=timeout,
            retries=retries,
        )
        setattr(target, "__agentkit_tool__", definition)
        return target

    return wrap
