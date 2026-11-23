"""R12 用户唯一入口 Agent 装配器（R12 §4 / P6）。

只做装配：持有 Arch/Program、能力级 Capability、可选 Port 覆盖；`build()` 在装配期
跑 static_check，拒绝有错架构，产出可直接交给 Runner 的 Session。
依赖只向下：runtime + compose（registry/notify/tool_layer），零领域语义、不下沉 R1。

版本纪律：架构名+版本写进 State.version（回放对齐），同时进 Session.version。
能力级开关：capability() 调 Capability.install(container=self, reg=self._reg)，
缺席即降级（未装的能力其 Port 不被注册，交给调用方 fail-closed）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import cast

from runtime import AgentState, Effect, Program, Session

from .registry import Capability, Registry

# 装配期只依赖协议形状，不在 compose 引入 archs（依赖方向：archs → compose）。
type _ArchLike = object  # Arch 协议或 Callable[[], Program]，由 runtime_checkable 协议在装配期校验

__all__ = ["Agent"]

class Agent:
    """底层装配器：arch + capability，build 产 Session。

    能力经 capability() 真 install 进 Registry（build 携带）；RAG/记忆/可观测/事件流
    一律走 api.Agent 的 use(Capability) + 钩子注入，本装配器不设死注入字段。
    """
    name = "agent"
    version = "12.0.0"

    def __init__(self, arch: _ArchLike) -> None:  # purity-exempt: Arch 协议与 ProgramFn 联合形态，compose 不反向依赖 archs
        """构造装配器。

        Args:
            arch: Arch（有 static_check/compile）或直接 Program 工厂（Callable[[], Program]）。
        """
        self._arch = arch
        self._reg = Registry()

    def capability(self, cap: Capability) -> "Agent":
        """装一个能力（install 注册其 Port 到共用 Registry）。"""
        cap.install(self, self._reg)
        return self

    def registry(self) -> Registry:
        """返回共用 Registry（装配期核对已注册 Port）。"""
        return self._reg

    def static_check(self) -> object:  # purity-exempt: Arch.static_check 返回 StaticCheckReport，compose 不反向依赖 archs
        """委托 Arch.static_check（无 Arch 时返回 ok 报告）。"""
        check = getattr(self._arch, "static_check", None)
        return check() if callable(check) else None

    def build(self, *, session_id: str = "agent") -> Session:
        """装配期校验 + 产 Session；有错误码时 ValueError 拒绝进运行期。

        Args:
            session_id: 会话标识。

        Returns:
            可直接交给 Runner.run 的 Session。

        Raises:
            ValueError: static_check 报告有 error（装配期拒绝）。
        """
        report = self.static_check()
        errors = getattr(report, "errors", ()) if report is not None else ()
        if errors:
            codes = ", ".join(str(getattr(e, "code", e)) for e in errors)
            raise ValueError(f"static_check failed: {codes}")

        factory = self._factory()
        arch_name = str(getattr(self._arch, "name", "program"))
        arch_version = str(getattr(self._arch, "version", "0.0.0"))
        version = f"{arch_name}@{arch_version}"
        state = AgentState().with_(version=version)
        return Session(session_id=session_id, program_factory=factory,
                       state=state, version=version)

    def _factory(self) -> Callable[[], Program]:
        """取 Program 工厂：Arch.compile 或 Callable[[], Program]。"""
        compile_fn = getattr(self._arch, "compile", None)
        if callable(compile_fn):
            return cast(Callable[[], Program], compile_fn)
        if callable(self._arch):
            return cast(Callable[[], Program], self._arch)
        raise TypeError(f"arch 不可编译: {self._arch!r}")
