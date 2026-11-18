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

from hooks import DangerLevel, ToolSpec
from runtime import DynamicValue

from .types import SandboxPolicy, ToolSource

__all__ = ["ToolFn", "tool"]


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


def tool(
    *,
    name: str | None = None,
    danger: DangerLevel,
    sandbox: SandboxPolicy = SandboxPolicy.AUTO,
    timeout: float = 30.0,
    retries: int = 2,
) -> Callable[[Callable[..., DynamicValue]], ToolFn]:
    """装饰器工厂：生成带 ToolSpec 的 ToolFn。

    参数：
        name: 工具名；缺省取函数名。danger: 风险等级（必填，决定路由）。
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

        if model_cls is not None:
            schema = model_cls.model_json_schema()

            def model_wrapper(**kwargs: DynamicValue) -> DynamicValue:
                # 为何：pydantic 模型以 extra=forbid 构造，脏参数在实现执行前就被挡下。
                return original(model_cls(**kwargs))

            fn: Callable[..., DynamicValue] = model_wrapper
        else:
            schema = {"type": "object", "properties": {}}

            def plain_wrapper(**kwargs: DynamicValue) -> DynamicValue:
                # 为何：无参/自由 kwargs 工具不约束字段，透传即可；
                # registry 层按空 properties 统一做 extra=forbid（见 registry.call）。
                return original(**kwargs)

            fn = plain_wrapper

        spec = ToolSpec(
            name=name or meta.__name__,
            description=_first_line(meta.__doc__),
            parameters_schema=schema,
            danger=danger,
            source=ToolSource.LOCAL.value,
        )
        return ToolFn(spec=spec, fn=fn)

    return decorate
