"""M2 链路契约：七挂载点 / 四泳道 / Verdict 判决 / 上下文与结果信封。

依赖仅 runtime/types.py；本模块不 import M3（Port 通过 ctx.invoke 注入）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Protocol, runtime_checkable

from agentkit.runtime import AgentState, Budgets, Effect, PortError, Result

__all__ = [
    "MountPoint", "VerdictAction", "Verdict", "ChainCtx", "ChainEvent",
    "DispatchResult", "Named", "Middleware", "Filter", "Pipe", "Hook",
    "NextFn", "ChainConfigError", "require_name",
]

VerdictAction = Literal["allow", "redact", "reject", "retry", "skip", "degrade", "abort"]


class MountPoint(str, Enum):
    ON_INPUT = "on_input"
    BEFORE_LLM = "before_llm"
    AFTER_LLM = "after_llm"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    ON_STATE = "on_state"
    ON_OUTPUT = "on_output"


@dataclass(frozen=True)
class Verdict:
    """filter 判决；data 依 action 语义：redact 的新数据 / skip 的 fallback / degrade 的目标 port。"""

    action: VerdictAction
    data: Any = None
    retry: int = 0
    backoff: float = 0.0
    reason: str = ""

    @classmethod
    def allow(cls) -> "Verdict":
        return cls("allow")

    @classmethod
    def reject(cls, reason: str = "rejected by filter") -> "Verdict":
        return cls("reject", reason=reason)


@runtime_checkable
class Named(Protocol):
    name: str


@runtime_checkable
class Middleware(Protocol):
    name: str

    async def __call__(self, ctx: "ChainCtx", nxt: "NextFn") -> Any: ...


@runtime_checkable
class Filter(Protocol):
    name: str

    def check(self, ctx: "ChainCtx", phase: Literal["request", "response"]) -> Verdict: ...


@runtime_checkable
class Pipe(Protocol):
    name: str

    def transform(self, data: Any, ctx: "ChainCtx") -> Any: ...


@runtime_checkable
class Hook(Protocol):
    name: str

    async def on_event(self, event: "ChainEvent", ctx: "ChainCtx") -> None: ...


NextFn = Callable[[], Awaitable[Result]]


@dataclass
class ChainCtx:
    """一次 dispatch 的上下文：state_view 只读，禁止组件修改 State。"""

    effect: Effect
    state_view: AgentState
    session_id: str
    mount: MountPoint = MountPoint.ON_INPUT
    attempt: int = 0
    budget_left: Budgets | None = None
    trace: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    invoke: Callable[[Effect, "ChainCtx"], Awaitable[Result]] | None = None


@dataclass(frozen=True)
class ChainEvent:
    """hook 收到的只读事件（M8 审计 / M9 可观测采集它）。"""

    mount: MountPoint
    phase: Literal["request", "response"]
    component: str
    effect: Effect
    data: Any = None
    error: PortError | None = None
    ts: int = 0  # 逻辑时钟（= attempt），不读墙钟


@dataclass(frozen=True)
class DispatchResult:
    """dispatch 返回：判决、结果、组件留名 trace、是否终止整个 run。"""

    effect: Effect
    verdict: Verdict
    data: Result | None = None
    trace: tuple[str, ...] = ()
    abort: bool = False


class ChainConfigError(Exception):
    """装配期 / 红线违规（无 name 组件、非法挂载点、未声明短路）。"""


def require_name(component: Any, kind: str) -> str:
    name = getattr(component, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ChainConfigError(
            f"{kind} component {type(component).__name__} must expose a non-empty .name "
            "(M2 红线：组件必须留名进 trace)"
        )
    return name
