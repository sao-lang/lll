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
    "Capability",
    "Decision",
    "DecisionAction",
    "Hook",
    "HookBusError",
    "HookCtx",
    "HookMode",
    "LifecycleEvent",
    "Matcher",
    "Provider",
    "Registry",
]


class HookMode(str, Enum):
    """Hook 执行语义：四泳道收敛为四种 mode。"""

    OBSERVE = "observe"  # 并行、异常隔离、返回忽略（原 hook）：只观察，不改流程
    GUARD = "guard"  # 串行、按优先级、首个非 continue 短路（原 filter）：把关拦截
    TRANSFORM = "transform"  # 串行、链式改写 payload/context（原 pipe）：改写上下文
    AROUND = "around"  # 包裹一次 invoke，拿到 next 回调（原 middleware）：洋葱包裹


class DecisionAction(str, Enum):
    """Hook 统一返回的动作；由 R1 内核执行。

    成员：
        CONTINUE: 放行。REWRITE: 改写。DENY: 拒绝。ASK: 转人工。
        RETRY: 重试。DEGRADE: 降级。FALLBACK: 兜底。ABORT: 终止。
    说明：
        当前 R1 内核真正消费的是 continue/deny/abort/fallback；
        rewrite/ask/retry/degrade 属 R3/R4/R9 未实现模块的动作，载体已定义。
    """

    CONTINUE = "continue"
    REWRITE = "rewrite"
    DENY = "deny"
    ASK = "ask"
    RETRY = "retry"
    DEGRADE = "degrade"
    FALLBACK = "fallback"
    ABORT = "abort"


class LifecycleEvent(str, Enum):
    """生命周期挂载点：session/turn/context/invoke/tool/state/subagent/external 八组。

    成员按阶段分组：
        session 级：SESSION_START / SESSION_END。
        prompt/turn 级：USER_PROMPT / BEFORE_MODEL / AFTER_MODEL / STOP / STOP_FAILURE。
        上下文级：BEFORE_COMPACT / AFTER_COMPACT。
        invoke 级：BEFORE_INVOKE / AFTER_INVOKE / INVOKE_ERROR。
        tool 级：BEFORE_TOOL / AFTER_TOOL / TOOL_ERROR。
        state 级：STATE_FOLD / BUDGET_CHECK。
        subagent 级：CHILD_SPAWN / CHILD_DONE。
        external 级：CONFIG_CHANGE / FILE_CHANGE。
    """

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
    """重试决策参数（R3 消费；R2 只定义载体）。

    字段：
        max_attempts: 最大尝试次数。base_delay: 初始退避秒数。
        multiplier: 退避倍数。max_delay: 退避上限秒数。
        jitter: 抖动策略（如 full）。respect_retry_after: 是否遵守服务端 Retry-After。
        idempotency_required: 是否要求幂等键（R3 据此拒绝非幂等重试）。
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 30.0
    jitter: str = "full"
    respect_retry_after: bool = True
    idempotency_required: bool = True


@dataclass(frozen=True)
class Decision:
    """Hook 统一返回：action 驱动 R1 行为，其余字段是各 action 的参数。

    字段（都是某类 action 的参数载体）：
        action: 裁决动作（默认 continue）。
        payload_patch: rewrite 用，待合并进 Effect.payload。
        context_inject: rewrite 用，注入的上下文文本。
        state_patch: 状态改写（R1 state frozen，尚未实现）。
        target: degrade 用，降级目标。
        value: 统一裁决载体——fallback 的兜底值 / around 中心 invoke 的 Result。
        retry: retry_ 用的重试参数。
        reason: 拒绝/终止/挂起的原因文本。
    """

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
        """放行：不改变执行流程。"""
        return cls(action=DecisionAction.CONTINUE)

    @classmethod
    def deny(cls, reason: str = "") -> Decision:
        """拒绝：不调用 port，返回失败。

        参数：
            reason: 拒绝原因。
        """
        return cls(action=DecisionAction.DENY, reason=reason)

    @classmethod
    def rewrite(
        cls,
        *,
        context: str | None = None,
        payload: dict[str, DynamicValue] | None = None,
    ) -> Decision:
        """改写：注入上下文或改写 payload 后继续。

        参数：
            context: 注入的上下文文本。payload: 待合并的 payload 补丁。
        """
        return cls(
            action=DecisionAction.REWRITE, context_inject=context, payload_patch=payload
        )

    @classmethod
    def retry_(cls, spec: RetrySpec | None = None) -> Decision:
        """重放当前 Effect（类名方法带下划线后缀以避开同名字段 retry）。

        参数：
            spec: 重试参数；缺省用 RetrySpec 默认值。
        """
        return cls(action=DecisionAction.RETRY, retry=spec or RetrySpec())

    @classmethod
    def degrade(cls, target: str) -> Decision:
        """换目标重跑。

        参数：
            target: 降级目标（如备用模型/port 名）。
        """
        return cls(action=DecisionAction.DEGRADE, target=target)

    @classmethod
    def fallback(cls, value: DynamicValue) -> Decision:
        """兜底：不调用 port，用 value 作为结果。

        参数：
            value: 兜底返回值。
        """
        return cls(action=DecisionAction.FALLBACK, value=value)

    @classmethod
    def ask(cls, question: str, options: list[str] | None = None) -> Decision:
        """转人工挂起。

        参数：
            question: 提问文本（存 reason）。options: 候选项（存 value）。
        """
        return cls(action=DecisionAction.ASK, reason=question, value=options)

    @classmethod
    def abort(cls, reason: str = "") -> Decision:
        """终止 run。

        参数：
            reason: 终止原因。
        """
        return cls(action=DecisionAction.ABORT, reason=reason)


@dataclass
class HookCtx:
    """Hook 收到的上下文：R1 RuntimeCtx 的超集。

    字段：
        effect: 当前 Effect（transform 可改）。state: 只读状态（State 本身 frozen）。
        session_id: 会话标识。event: 当前事件名（由 bus.emit 注入）。
        attempt: 尝试次数。budget_left: 预算余量投影。
        trace: hook 执行轨迹。meta: 临时元数据（around 的 next 回调存这里）。
        invoke: 由 R1 注入的 Port 调用回调（洋葱中心使用）。
    """

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
        """只读状态视图（State 本身 frozen，故直接返回即可）。"""
        return self.state


NextFn = Callable[[], Awaitable[Decision]]
"""around 洋葱的"下一层"回调类型；供 hook 通过 ctx.meta['next'] 取得。"""


@runtime_checkable
class Hook(Protocol):
    """唯一扩展点：订阅事件 + mode + 返回 Decision。

    属性：
        name: 唯一名称（强制留名）。events: 订阅的事件名元组。
        mode: 执行语义。priority: 优先级（小者先）。
        requires_ports: 依赖的 port 名（装配期校验）。
    """

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
    """事件匹配器：按 kind/tool/model 过滤（当前 bus 未强制调用，供插件自选）。

    字段：
        kinds: 允许的 Effect kind 值；空表示不限。
        tools: 允许的工具名；空表示不限。
        models: 允许的模型名；空表示不限。
    """

    kinds: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    def matches(self, ctx: HookCtx) -> bool:
        """判断当前 ctx 是否命中；空条件表示全匹配。

        参数：
            ctx: 钩子上下文，读取其 effect 做过滤。
        返回：
            命中 True，否则 False。
        """
        effect = ctx.effect
        if self.kinds and effect.kind.value not in self.kinds:
            return False
        # 工具名优先取 payload.call 内的 name，回退到 payload.name。
        if (
            self.tools
            and str(effect.payload.get("call") or effect.payload.get("name") or "")
            not in self.tools
        ):
            return False
        return not (self.models and str(effect.payload.get("model") or "") not in self.models)


@runtime_checkable
class Provider(Protocol):
    """所有可替换策略的基契约：带 name、可被替换。"""

    name: str


@runtime_checkable
class Capability(Protocol):
    """能力级开关：装了通过 install 生效，不装缺席（缺席即降级）。

    属性：
        name: 能力名。version: 版本。requires: 依赖的其他能力名。
    """

    name: str
    version: str
    requires: tuple[str, ...]

    def install(self, bus: HookBus, reg: Registry) -> None:
        """把本能力的所有 hook 装到总线、provider 装到注册表。

        参数：
            bus: 钩子总线。reg: Provider 注册表。
        """
        ...


class HookBusError(Exception):
    """R2 装配/执行错误（重名、缺 port、冻结后写入等）。"""

    code = "HOOKBUS_ERROR"  # 错误码，便于上层折叠为 Result


class Registry:
    """注册表：name → Provider，支持注册/解析/替换；装配期可 freeze 拒绝后续写入。"""

    def __init__(self) -> None:
        """初始化空注册表，装配期 freeze 后拒绝任何写入。"""
        self._providers: dict[str, Provider] = {}  # name → provider 实例
        self._frozen = False  # 冻结标志（装配完成后置 True）

    def register(self, name: str, provider: Provider) -> Registry:
        """注册 provider；重名报错（除非用 replace）；返回 self 支持链式。

        参数：
            name: provider 名。provider: 实现 Provider 契约的对象。
        返回：
            self（链式调用）。
        异常：
            HookBusError: 已冻结或重名。
        """
        if self._frozen:
            raise HookBusError("registry is frozen")
        if name in self._providers:
            raise HookBusError(f"duplicate provider name: {name}")
        self._providers[name] = provider
        return self

    def resolve(self, name: str) -> Provider:
        """解析 provider；缺失报错。

        参数：
            name: provider 名。
        返回：
            已注册的 provider。
        异常：
            HookBusError: 未注册。
        """
        if name not in self._providers:
            raise HookBusError(f"provider not registered: {name}")
        return self._providers[name]

    def replace(self, name: str, provider: Provider) -> Registry:
        """替换已注册的 provider；返回 self 支持链式。

        参数：
            name: 已存在的 provider 名。provider: 新实现。
        返回：
            self（链式调用）。
        异常：
            HookBusError: 已冻结。
        """
        if self._frozen:
            raise HookBusError("registry is frozen")
        self._providers[name] = provider
        return self

    def has(self, name: str) -> bool:
        """是否已注册该 name。"""
        return name in self._providers

    def names(self) -> Iterable[str]:
        """已注册名称列表（快照）。"""
        return list(self._providers)

    def freeze(self) -> Registry:
        """冻结注册表（装配期完成后），此后 register/replace 均拒绝。"""
        self._frozen = True
        return self
