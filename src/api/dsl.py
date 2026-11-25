"""用户封装自定义架构的 FP 组合入口（方案 A，用户 15:39 确认）。

设计（契约②：组合式 API 封装架构、要 FP；红队 B3 方案 A）：
- 用户直接用 compose 的 A 类 FP 算子（签名 ProgramFn→ProgramFn 纯变换）组合自己的架构；
- Effect 是组合单元，对用户公开（用户能写带 tool_calls 决策的循环，与内置架构同权）；
- 只有 message/payload 这类**数据**由语义门面（api.Agent.say）内部注入，用户不手拼。

内置 10 架构（archs）与本模块导出的算子是同一套组合机制：内置用它们直编，
用户用它们封装自己的架构，产出的 ProgramFn 同形，可装进 api.Agent 或 Runner。

不造语义块式封装（契约已否决）；本模块是**算子的公开重导出 + 架构封装助手**。
"""
from __future__ import annotations

from collections.abc import Callable

from archs.base import Arch, StaticCheckIssue, StaticCheckReport
from compose.program import (
    JoinMode,
    ProgramFn,
    branch_value,
    fallback,
    loop_until,
    map_effect,
    par,
    race,
    retry,
    seq,
)
from runtime import Program

__all__ = [
    "JoinMode",
    "ProgramFn",
    "branch_value",
    # 架构封装助手
    "custom_arch",
    "fallback",
    "loop_until",
    "map_effect",
    "par",
    "race",
    "retry",
    # FP 组合算子（原样重导出，ProgramFn→ProgramFn 纯变换）
    "seq",
]



class _CustomArch:
    """用户自定义架构（Arch 协议实现），由 custom_arch 构造。"""
    def __init__(self, *, name: str, version: str,
                 check_fn: Callable[[], StaticCheckReport],
                 factory: Callable[[], Program]) -> None:
        self.name = name
        self.version = version
        self._check_fn = check_fn
        self._factory = factory

    def static_check(self) -> StaticCheckReport:
        return self._check_fn()

    def compile(self) -> Program:
        return self._factory()

def custom_arch(
    factory: Callable[[], Program],
    *,
    name: str = "custom",
    version: str = "1.0.0",
    requires: tuple[str, ...] = (),
    static_check: Callable[[], StaticCheckReport] | None = None,
) -> Arch:
    """把用户用 FP 算子组合出的 ProgramFn 封装成内置同形的 Arch。

    用户路径（与内置架构同权）：
        from api import dsl
        from runtime import Invoke, Terminate

        def my_program():
            out = yield Invoke(port="llm", payload={"messages": [...]})
            yield Terminate(output=out)

        MyArch = dsl.custom_arch(my_program, name="my-arch")
        # 可装进 api.Agent(MyArch) 或 Runner，和 ReAct 同形

    Args:
        factory: 用户组合出的 Program 工厂（Callable[[], Program]）。
        name/version: 架构标识（进 State.version，回放对齐）。
        requires: 需要的 port 名。
        static_check: 自定义校验器；缺省用最小默认（非空 factory 即通过）。

    Returns:
        实现 Arch 协议的对象（name/version/static_check/compile）。
    """
    def default_check() -> StaticCheckReport:
        errs = () if callable(factory) else (StaticCheckIssue(
            "CUSTOM-E01", "factory 不可调用", "factory"),)
        return StaticCheckReport(errors=errs, arch=name, version=version,
                                 ports_required=requires)

    check = static_check or default_check

    return _CustomArch(name=name, version=version, check_fn=check, factory=factory)
