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

type _Port = object
type _Stream = object


class Agent:
    """用户装配器：arch + capability + 可选 Port/流，build 产 Session。

    链式 API 对应 R12 §4：capability/rag/memory/telemetry/event_stream/build。
    所有注入只登记到内部 Registry 或持有引用，不在此实现任何具体能力。
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
        self._overrides: dict[str, _Port] = {}
        self._rag: _Port | None = None
        self._memory: _Port | None = None
        self._telemetry: _Port | None = None
        self._stream: _Stream | None = None

    def capability(self, cap: Capability) -> "Agent":
        """装一个能力（install 注册其 Port 到共用 Registry）。"""
        cap.install(self, self._reg)
        return self

    def rag(self, pipeline: _Port) -> "Agent":
        """注入 RAG pipeline（实例级覆盖）。"""
        self._rag = pipeline
        return self

    def memory(self, port: _Port) -> "Agent":
        """注入 MemoryPort。"""
        self._memory = port
        self._overrides["memory"] = port
        return self

    def telemetry(self, port: _Port) -> "Agent":
        """注入 TelemetryPort。"""
        self._telemetry = port
        self._overrides["telemetry"] = port
        return self

    def event_stream(self, stream: _Stream) -> "Agent":
        """注入事件流（订阅端）。"""
        self._stream = stream
        self._overrides["event_stream"] = stream
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
