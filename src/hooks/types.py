"""R2 插件总线核心类型：唯一扩展点 Hook + 统一返回 Decision + 注册表抽象。

契约（R2 §4）：
- 一种 Hook（mode 保留 observe/guard/transform/around 四语义），一种 Decision（action 统一返回）。
- 对外只有 bus.on() 一个注册入口——没有 middleware/filter/pipe 三套 API。
- 一切可插拔物强制留名（name），装配期校验。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from runtime import AgentState, Budgets, DynamicValue, Effect, Result

if TYPE_CHECKING:  # 避免运行期循环 import（bus 反向依赖本模块）
    from .bus import HookBus

__all__ = [
    "HookMode",
    "DecisionAction",
    "Decision",
    "HookCtx",
    "Hook",
    "Matcher",
    "Capability",
    "Provider",
    "Registry",
    "HookBusError",
    "LifecycleEvent",
]


class HookMode(str, Enum):
    """Hook 执行语义：四泳道收敛为四种 mode。"""

    OBSERVE = "observe"  # 并行、异常隔离、返回忽略（原 hook）
    GUARD = "guard"  # 串行、按优先级、首个非 continue 短路（原 filter）
    TRANSFORM = "transform"  # 串行、链式改写 payload/context（原 pipe）
    AROUND = "around"  # 包裹一次 invoke，拿到 next 回调（原 middleware）


class DecisionAction(str, Enum):
    """Hook 统一返回的动作；由 R1 内核执行。"""

    CONTINUE = "continue"
    REWRITE = "rewrite"
    DENY = "deny"
    ASK = "ask"
    RETRY = "retry"
    DEGRADE = "degrade"
    FALLBACK = "fallback"
    ABORT = "abort"


class LifecycleEvent(str, Enum):
    """生命周期挂载点：session/turn/context/invoke/tool/state/subagent/external。"""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT = "user_prompt"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    STOP = "stop"
    STOP_FAILURE = "stop_failure"
    BEFORE_COMPACT = "before_compact"
    AFTER_COMPACT = "after_compact"
    BEFORE_INVOKE = "before_invoke"
    AFTER_INVOKE = "after_invoke"
    INVOKE_ERROR = "invoke_error"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    TOOL_ERROR = "tool_error"
    STATE_FOLD = "state_fold"
    BUDGET_CHECK = "budget_check"
    CHILD_SPAWN = "child_spawn"
    CHILD_DONE = "child_done"
    CONFIG_CHANGE = "config_change"
    FILE_CHANGE = "file_change"


@dataclass(frozen=True)
class RetrySpec:
    """重试决策参数（R3 消费；R2 只定义载体）。"""

    max_attempts: int = 3
    base_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 30.0
    jitter: str = "full"
    respect_retry_after: bool = True
    idempotency_required: bool = True


@dataclass(frozen=True)
class Decision:
    """Hook 统一返回：action 驱动 R1 行为，其余字段是各 action 的参数。"""

    action: DecisionAction = DecisionAction.CONTINUE
    payload_patch: dict[str, DynamicValue] | None = None
    context_inject: str | None = None
    state_patch: dict[str, DynamicValue] | None = None
    target: str | None = None
    value: DynamicValue = None
    retry: RetrySpec | None = None
    reason: str = ""

    @classmethod
    def continue_(cls) -> Decision:
        """放行。"""
        return cls(action=DecisionAction.CONTINUE)

    @classmethod
    def deny(cls, reason: str = "") -> Decision:
        """拒绝：不调用 port。"""
        return cls(action=DecisionAction.DENY, reason=reason)

    @classmethod
    def rewrite(
        cls,
        *,
        context: str | None = None,
        payload: dict[str, DynamicValue] | None = None,
    ) -> Decision:
        """改写：注入上下文或改写 payload 后继续。"""
        return cls(
            action=DecisionAction.REWRITE, context_inject=context, payload_patch=payload
        )

    @classmethod
    def retry_(cls, spec: RetrySpec | None = None) -> Decision:
        """重放当前 Effect（类名方法带下划线后缀以避开同名字段 retry）。"""
        return cls(action=DecisionAction.RETRY, retry=spec or RetrySpec())

    @classmethod
    def degrade(cls, target: str) -> Decision:
        """换目标重跑。"""
        return cls(action=DecisionAction.DEGRADE, target=target)

    @classmethod
    def fallback(cls, value: DynamicValue) -> Decision:
        """兜底：不调用 port，用 value 作为结果。"""
        return cls(action=DecisionAction.FALLBACK, value=value)

    @classmethod
    def ask(cls, question: str, options: list[str] | None = None) -> Decision:
        """转人工挂起。"""
        return cls(action=DecisionAction.ASK, reason=question, value=options)

    @classmethod
    def abort(cls, reason: str = "") -> Decision:
        """终止 run。"""
        return cls(action=DecisionAction.ABORT, reason=reason)


@dataclass
class HookCtx:
    """Hook 收到的上下文：R1 RuntimeCtx 的超集。"""

    effect: Effect
    state: AgentState
    session_id: str
    event: str
    attempt: int = 0
    budget_left: Budgets | None = None
    trace: list[str] = field(default_factory=list)
    meta: dict[str, DynamicValue] = field(default_factory=dict)
    invoke: Callable[[Effect, HookCtx], Awaitable[Result]] | None = None

    @property
    def state_view(self) -> AgentState:
        """只读状态视图（State 本身 frozen）。"""
        return self.state


NextFn = Callable[[], Awaitable[Decision]]


@runtime_checkable
class Hook(Protocol):
    """唯一扩展点：订阅事件 + mode + 返回 Decision。"""

    name: str
    events: tuple[str, ...]
    mode: HookMode
    priority: int
    requires_ports: tuple[str, ...]

    async def __call__(self, ctx: HookCtx) -> Decision:
        """执行插件逻辑并返回 Decision（异常由总线折叠为 continue）。"""
        ...


@dataclass(frozen=True)
class Matcher:
    """事件匹配器：按 kind/tool/model/danger 过滤。"""

    kinds: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    def matches(self, ctx: HookCtx) -> bool:
        """判断当前 ctx 是否命中；空条件表示全匹配。"""
        effect = ctx.effect
        if self.kinds and effect.kind.value not in self.kinds:
            return False
        if (
            self.tools
            and str(effect.payload.get("call") or effect.payload.get("name") or "")
            not in self.tools
        ):
            return False
        if self.models and str(effect.payload.get("model") or "") not in self.models:
            return False
        return True


@runtime_checkable
class Provider(Protocol):
    """所有可替换策略的基契约：带 name、可被替换。"""

    name: str


@runtime_checkable
class Capability(Protocol):
    """能力级开关：装了通过 install 生效，不装缺席。"""

    name: str
    version: str
    requires: tuple[str, ...]

    def install(self, bus: HookBus, reg: Registry) -> None:
        """把本能力的所有 hook 装到总线、provider 装到注册表。"""
        ...


class HookBusError(Exception):
    """R2 装配/执行错误。"""

    code = "HOOKBUS_ERROR"


class Registry:
    """注册表：name → Provider，支持注册/解析/替换。"""

    def __init__(self) -> None:
        """初始化空注册表，装配期 freeze 后拒绝任何写入。"""
        self._providers: dict[str, Provider] = {}
        self._frozen = False

    def register(self, name: str, provider: Provider) -> Registry:
        """注册 provider；重名报错（除非用 replace）；返回 self 支持链式。"""
        if self._frozen:
            raise HookBusError("registry is frozen")
        if name in self._providers:
            raise HookBusError(f"duplicate provider name: {name}")
        self._providers[name] = provider
        return self

    def resolve(self, name: str) -> Provider:
        """解析 provider；缺失报错。"""
        if name not in self._providers:
            raise HookBusError(f"provider not registered: {name}")
        return self._providers[name]

    def replace(self, name: str, provider: Provider) -> Registry:
        """替换已注册的 provider；返回 self 支持链式。"""
        if self._frozen:
            raise HookBusError("registry is frozen")
        self._providers[name] = provider
        return self

    def has(self, name: str) -> bool:
        """是否已注册。"""
        return name in self._providers

    def names(self) -> Iterable[str]:
        """已注册名称列表。"""
        return list(self._providers)

    def freeze(self) -> Registry:
        """冻结注册表（装配期完成后）。"""
        self._frozen = True
        return self
