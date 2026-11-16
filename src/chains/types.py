"""M2 链路契约：七挂载点 / 四泳道 / Verdict 判决 / 上下文与结果信封。

依赖仅 runtime/types.py；本模块不 import M3（Port 通过 ctx.invoke 注入）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, TypedDict, cast, runtime_checkable

from runtime import AgentState, Budgets, DynamicValue, Effect, PortError, Result

__all__ = [
    "MountPoint", "VerdictAction", "Verdict", "ChainCtx", "ChainCtxMeta", "ChainEvent",
    "DispatchResult", "Named", "Middleware", "Filter", "Pipe", "Hook",
    "NextFn", "ChainConfigError", "require_name",
]


class ChainCtxMeta(TypedDict, total=False):
    """ChainCtx.meta 中由框架写入的固定键；组件也可写自定义键（运行时 dict 开放）。"""

    error: PortError | None  # 本步 port 错误（回程 filter 的 retry/skip 判据）
    request_data: DynamicValue  # redact 后留在请求侧的数据
    degrade_port: DynamicValue  # 降级目标 port 名，由 call 读取
    run_id: str  # 跨周期运行标识，折叠进 PortCtx

VerdictAction = Literal["allow", "redact", "reject", "retry", "skip", "degrade", "abort"]
"""filter 的七种判决动作；每个值都由 Chains.dispatch 明确处理。"""


class MountPoint(str, Enum):
    """链路的七个挂载点；LLM/TOOL 各有专用前后点，其余 kind 走通用进出点。"""

    ON_INPUT = "on_input"  # 通用入口级挂载
    BEFORE_LLM = "before_llm"  # LLM 调用前
    AFTER_LLM = "after_llm"  # LLM 调用后
    BEFORE_TOOL = "before_tool"  # 工具调用前
    AFTER_TOOL = "after_tool"  # 工具调用后
    ON_STATE = "on_state"  # 状态压缩 / 上下文整理
    ON_OUTPUT = "on_output"  # 通用出口级挂载


@dataclass(frozen=True)
class Verdict:
    """filter 判决；data 依 action 语义：redact 的新数据 / skip 的 fallback / degrade 的目标 port。"""

    action: VerdictAction  # 判决动作；决定链路后续走向
    data: DynamicValue = None  # 动作载荷：redact 的新数据 / skip 的 fallback / degrade 的目标 port
    retry: int = 0  # retry 动作的重试次数
    backoff: float = 0.0  # retry 前的退避秒数
    reason: str = ""  # 判决原因，进错误信封与 trace

    @classmethod
    def allow(cls) -> Verdict:
        """放行判决：链路继续正常执行。"""
        return cls("allow")

    @classmethod
    def reject(cls, reason: str = "rejected by filter") -> Verdict:
        """拒绝判决：短路为 FILTER_REJECTED 失败信封。"""
        return cls("reject", reason=reason)


@runtime_checkable
class Named(Protocol):
    """组件须留名的透明性红线协议；装配期由 require_name 校验。"""

    name: str  # 组件名，进 trace


@runtime_checkable
class Middleware(Protocol):
    """洋葱中间件协议：包裹 nxt 调用，返回 Result 或 None（None 表示透传）。"""

    name: str  # 组件名

    async def __call__(self, ctx: ChainCtx, nxt: NextFn) -> DynamicValue: ...


@runtime_checkable
class Filter(Protocol):
    """护栏协议：对 request / response 两阶段做判决。"""

    name: str  # 组件名

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict: ...


@runtime_checkable
class Pipe(Protocol):
    """纯数据变换协议：去程改 payload、回程改 result.data。"""

    name: str  # 组件名

    def transform(self, data: DynamicValue, ctx: ChainCtx) -> DynamicValue: ...


@runtime_checkable
class Hook(Protocol):
    """只读观察者协议：异常须被吞掉，不得影响主流程。"""

    name: str  # 组件名

    async def on_event(self, event: ChainEvent, ctx: ChainCtx) -> None: ...


NextFn = Callable[[], Awaitable[Result]]
"""中间件调用下一层的无参回调；返回内层 Result。"""


@dataclass
class ChainCtx:
    """一次 dispatch 的上下文：state_view 只读，禁止组件修改 State。"""

    effect: Effect  # 本次 dispatch 的 Effect（可被 pipe/redact 替换）
    state_view: AgentState  # 只读状态视图；禁止组件修改
    session_id: str  # 会话标识
    mount: MountPoint = MountPoint.ON_INPUT  # 当前挂载点，由调度器推进
    attempt: int = 0  # 当前重试次数
    budget_left: Budgets | None = None  # 剩余预算快照
    trace: list[str] = field(default_factory=list)  # 组件留名轨迹
    meta: ChainCtxMeta = field(default_factory=lambda: cast(ChainCtxMeta, {}))  # 组件间临时元数据，固定键见 ChainCtxMeta
    invoke: Callable[[Effect, ChainCtx], Awaitable[Result]] | None = None  # Runner 注入的 M3 call


@dataclass(frozen=True)
class ChainEvent:
    """hook 收到的只读事件（M8 审计 / M9 可观测采集它）。"""

    mount: MountPoint  # 事件发生的挂载点
    phase: Literal["request", "response"]  # 去程 / 回程
    component: str  # 产生事件的组件名（留名）
    effect: Effect  # 对应的 Effect
    data: DynamicValue = None  # 事件携带的数据快照
    error: PortError | None = None  # 错误信封（若有）
    ts: int = 0  # 逻辑时钟（= attempt），不读墙钟


@dataclass(frozen=True)
class DispatchResult:
    """dispatch 返回：判决、结果、组件留名 trace、是否终止整个 run。"""

    effect: Effect  # 最终生效的 Effect（可能被 redact 替换）
    verdict: Verdict  # 终局判决
    data: Result | None = None  # port 调用结果（若有）
    trace: tuple[str, ...] = ()  # 组件留名轨迹
    abort: bool = False  # 是否终止整个 run


class ChainConfigError(Exception):
    """装配期 / 红线违规（无 name 组件、非法挂载点、未声明短路）。"""


def require_name(component: DynamicValue, kind: str) -> str:
    """校验组件暴露了非空 .name（M2 透明性红线）；否则抛 ChainConfigError。"""
    name = getattr(component, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ChainConfigError(
            f"{kind} component {type(component).__name__} must expose a non-empty .name "
            "(M2 红线：组件必须留名进 trace)"
        )
    return name
