"""R5 工具装饰器：把普通 Python 函数包装为带 ToolSpec 的 ToolFn。

关键纪律：
- danger 必填关键字（装配期强制，规则 5 用枚举约束取值）；
- 第一个参数为 pydantic BaseModel 时，用 model_json_schema() 生成 L2 schema，
  并借 pydantic 的 extra=forbid 在包装层拦截未声明参数；
- 无参 / 自由 **kwargs 函数生成空 properties，交给 registry 层统一做 extra=forbid。
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast, get_type_hints

from pydantic import BaseModel

from compose import ToolSpec

from .types import DangerLevel
from runtime import DynamicValue

from .types import SandboxPolicy, ToolSource

__all__ = ["ToolFn", "tool", "pending_tools", "clear_pending"]

# 【v2.2】模块级待注册容器：@tool 装饰即入列，装配期由 registry.register_pending 批量消费。
# 为何是模块级：零参数兼容现有全部 @tool 调用点；代价是全局状态，故提供 clear_pending 与
# tests/r5/conftest.py 的 autouse fixture 保证测试隔离。
_PENDING: list[ToolFn] = []


def pending_tools() -> tuple[ToolFn, ...]:
    """返回待注册工具的不可变快照（防外部改写内部容器）。

    Returns:
        按装饰顺序排列的 ToolFn 元组；无待注册项时为空元组。
    """
    return tuple(_PENDING)


def clear_pending() -> None:
    """清空待注册容器（供测试隔离与装配层显式重置）；不改变已注册工具。"""
    _PENDING.clear()


class _Named(Protocol):
    """被装饰函数的元数据视图：名字与文档由函数对象提供，供 cast 后安全读取。"""

    __name__: str
    __doc__: str | None


@dataclass(frozen=True, slots=True)
class ToolFn:
    """可调用工具：携带 ToolSpec 的 frozen 包装器（规则 1/2：完整注解 + slots）。

    字段：
        spec: 工具目录条目（name/description/schema/danger/source）。
        fn: 实际执行体，签名统一为 **kwargs -> DynamicValue。
    """

    spec: ToolSpec
    fn: Callable[..., DynamicValue]

    def __call__(self, **kwargs: DynamicValue) -> DynamicValue:
        """以关键字参数调用工具实现（参数校验由 pydantic 模型或 registry 完成）。"""
        return self.fn(**kwargs)


def _first_line(doc: str | None) -> str:
    """取 docstring 首行作为一句话描述（多行正文不进 L1 目录，避免披露膨胀）。"""
    if doc is None:  # 无 docstring 的函数返回空描述，不抛异常
        return ""
    stripped = doc.strip()
    if not stripped:  # 全空白文档等价于无描述
        return ""
    return stripped.splitlines()[0].strip()


_JSON_TYPES: dict[type, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    dict: "object", list: "array", tuple: "array", set: "array",
}


def _annotation_schema(annotation: DynamicValue) -> dict[str, DynamicValue]:
    """把单个 Python 类型注解推导为 JSON Schema 片段（逐参数推导，B8）。

    支持：基础类型、Optional[T]（nullable）、list[T]/dict 泛型、未知类型退回 string。
    """
    import types
    from typing import Union, get_args, get_origin

    origin = get_origin(annotation)
    args = get_args(annotation)
    origin_cmp = cast(object, origin)  # purity-exempt: 类型注解推导需与 typing 泛型对象比较，object 是唯一安全公共类型
    ann_cmp = cast(object, annotation)  # purity-exempt: 同上，注解可能是 type/typing 别名，cast object 做类型判别
    if origin_cmp is Union or isinstance(origin_cmp, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            sub = _annotation_schema(non_none[0])
            return {**sub, "type": [sub.get("type"), "null"]}
        return {"type": "string"}
    if origin_cmp in (list, tuple, set):
        item = _annotation_schema(args[0]) if args else {}
        return {"type": "array", "items": item}
    if origin_cmp is dict:
        return {"type": "object"}
    if ann_cmp in _JSON_TYPES and isinstance(ann_cmp, type):
        return {"type": _JSON_TYPES[ann_cmp]}
    return {"type": "string"}


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    danger: DangerLevel,
    sandbox: SandboxPolicy = SandboxPolicy.AUTO,
    timeout: float = 30.0,
    retries: int = 2,
) -> Callable[[Callable[..., DynamicValue]], ToolFn]:
    """装饰器工厂：生成带 ToolSpec 的 ToolFn。

    参数：
        name: 工具名；缺省取函数名。
        description: 一句话描述；缺省取 docstring 首行（显式声明优先）。
        danger: 风险等级（必填，决定路由）。
        sandbox/timeout/retries: 执行策略声明；仅供装配层读取，ToolSpec 不承载执行策略字段。
    返回：
        接受原函数、返回 ToolFn 的装饰器。
    """
    # sandbox/timeout/retries 是本装饰器的执行策略声明位，R9 执行外壳尚未定型，
    # 故当前只接收不写入 ToolSpec（避免往 DTO 塞未定语义的字段）。
    _ = (sandbox, timeout, retries)

    def decorate(original: Callable[..., DynamicValue]) -> ToolFn:
        """包装单个函数：解析首个参数注解，生成 schema 与统一 **kwargs 执行体。"""
        meta = cast(_Named, original)
        signature = inspect.signature(original)
        params = tuple(signature.parameters.values())
        hints = get_type_hints(original)
        first = params[0] if params else None
        model_cls: type[BaseModel] | None = None
        if first is not None:  # 有参数才需要判定是否为 pydantic 模型
            annotation = hints[first.name] if first.name in hints else None
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                # 首参是 BaseModel 子类：schema 由模型生成，校验也交给模型构造。
                model_cls = annotation

        schema: dict[str, DynamicValue]
        if model_cls is not None:
            schema = model_cls.model_json_schema()

            def model_wrapper(**kwargs: DynamicValue) -> DynamicValue:
                # 为何：pydantic 模型以 extra=forbid 构造，脏参数在实现执行前就被挡下。
                return original(model_cls(**kwargs))

            fn: Callable[..., DynamicValue] = model_wrapper
        else:
            # 逐参数类型注解 → JSON Schema（B8：city: str 等普通注解也推导）。
            properties: dict[str, DynamicValue] = {}
            required: list[str] = []
            has_var_kwargs = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params
            )
            for p in params:
                if p.name in ("self", "cls") or p.kind in (
                    inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD
                ):
                    continue
                hint = hints.get(p.name, p.annotation)
                if hint is inspect.Parameter.empty:
                    hint = object  # 无注解 → string 兜底
                properties[p.name] = _annotation_schema(hint)
                if p.default is inspect.Parameter.empty:
                    required.append(p.name)
            schema = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            # 自由 **kwargs 工具：无具名参数时不强加 extra=forbid（透传语义）。
            if has_var_kwargs and not properties:
                schema = {"type": "object", "properties": {}}

            def plain_wrapper(**kwargs: DynamicValue) -> DynamicValue:
                return original(**kwargs)

            fn = plain_wrapper

        spec = ToolSpec(
            name=name or meta.__name__,
            description=description if description is not None else _first_line(meta.__doc__),
            parameters_schema=schema,
            danger=danger,
            source=ToolSource.LOCAL.value,
        )
        tool_fn = ToolFn(spec=spec, fn=fn)
        _PENDING.append(tool_fn)  # 【v2.2】装配期自动注册的入列点；签名对调用方透明。
        return tool_fn

    return decorate
