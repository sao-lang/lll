"""Lifecycle 通知体系：16 个相位的纯通知总线。

三体系正交中的 "when" 维度：回答"发生了什么"，订阅者收到 Notice 只做事，
返回值框架不消费——不能拦截、不能改写、异常就地隔离。与 AOP Enhancer（how，
返回值被消费）严格分开。

依赖方向：订阅插件 → LifecycleBus → 内核通知；本模块不 import runtime/aop 实现，
Notice 的调用上下文以只读投影字段承载（结构化，避免循环依赖）。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查期引用，运行期不依赖 aop（避免循环）。
    from aop.invocation import Invocation

__all__ = [
    "PHASE_COUNT",
    "ErrorHook",
    "LifecycleBus",
    "LifecyclePhase",
    "Notice",
    "runner_notifier",
]


class LifecyclePhase(StrEnum):
    """16 个通知相位（最终口径，不得增删）。

    分组：
        会话/轮次：session_start / user_prompt / session_end / state_fold / budget_check
        能力调用：before_model / after_model / before_tool / after_tool / tool_error
        子 Agent：child_spawn / child_done
        控制流：cancelled / wait_human / resumed
        AOP 拒绝：invoke_rejected
    """
    SESSION_START = "session_start"
    USER_PROMPT = "user_prompt"
    SESSION_END = "session_end"
    STATE_FOLD = "state_fold"
    BUDGET_CHECK = "budget_check"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    TOOL_ERROR = "tool_error"
    CHILD_SPAWN = "child_spawn"
    CHILD_DONE = "child_done"
    CANCELLED = "cancelled"
    WAIT_HUMAN = "wait_human"
    RESUMED = "resumed"
    INVOKE_REJECTED = "invoke_rejected"


PHASE_COUNT = len(LifecyclePhase)
"""通知相位数量（16，口径自证的单一来源）。"""


@dataclass(frozen=True)
class Notice:
    """通知载荷：Invocation 的只读投影 + 相位名 + 专属 payload。

    字段：
        phase: 通知相位。
        session_id: 会话标识（从 Invocation.session/projection 派生）。
        port: 能力端口名（Invoke 场景；非能力相位为空串）。
        payload: 该相位的专属只读数据（如 budget_check 的 exceeded、拒绝的 reason）。
        meta: Invocation.meta 的只读快照（单一上下文，不另建状态）。
    """
    phase: LifecyclePhase
    session_id: str = ""
    port: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结 payload / meta 为只读视图（frozen 下用 object.__setattr__）。"""
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))

    @classmethod
    def from_invocation(
        cls,
        phase: LifecyclePhase,
        inv: Invocation,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Notice:
        """由 Invocation 投影出 Notice（上下文单一：Notice 只读，不复制可变状态）。

        参数：
            phase: 通知相位。
            inv: AOP 调用上下文（effect/ctx/session/meta）。
            payload: 该相位专属数据；缺省空。
        返回：
            只读 Notice。
        """
        return cls(
            phase=phase,
            session_id=str(getattr(inv.session, "session_id", "") or ""),
            port=str(getattr(inv.effect, "port", "") or ""),
            payload=payload or {},
            meta=inv.meta,
        )


type NoticeFn = Callable[[Notice], None]
"""订阅者签名：收 Notice、无返回值（返回值框架不消费）。"""

type ErrorHook = Callable[[str, LifecyclePhase, Exception], None]
"""订阅者异常钩子：(订阅者名, 相位, 异常)；由装配层决定是否接 R10 trace。"""


class LifecycleBus:
    """通知总线：按相位注册订阅者，emit 时异常就地隔离、不阻断其余订阅者。

    同步语义：emit 不返回 awaitable；订阅者是同步副作用（记录/观测/审计）。
    异常默认静默隔离；可选 on_error 钩子让装配层接可观测（R10）。
    """
    __slots__ = ("_on_error", "_subscribers")

    def __init__(self, on_error: ErrorHook | None = None) -> None:
        """初始化空订阅表（phase → 订阅者列表）。

        参数：
            on_error: 可选订阅者异常钩子；缺省 None 保持静默隔离（向后兼容）。
        """
        self._subscribers: dict[LifecyclePhase, list[tuple[str, NoticeFn]]] = {}
        self._on_error = on_error

    def on(self, phase: LifecyclePhase, fn: NoticeFn, *, name: str = "") -> LifecycleBus:
        """订阅一个相位。

        参数：
            phase: 目标相位。
            fn: 订阅回调（名字 + 签名 name + register(bus) 的插件持有自身状态）。
            name: 订阅者名（trace/诊断用）；缺省用函数名。
        返回：
            self（支持链式订阅）。
        """
        key = name or getattr(fn, "__name__", "anonymous")
        self._subscribers.setdefault(phase, []).append((key, fn))
        return self

    def emit(self, notice: Notice) -> None:
        """广播通知：异常就地隔离，保证一个坏订阅者不影响其余订阅与主流程。

        参数：
            notice: 通知载荷。
        """
        for name, fn in self._subscribers.get(notice.phase, ()):
            try:
                fn(notice)
            except Exception as exc:  # 通知型 hook：异常就地隔离，绝不抛穿  # noqa: BLE001
                # 通知不消费返回值、不拦截主流程；订阅者自身的错误是其内部责任。
                # 可选钩子把异常交给装配层观测（R10 trace），默认 None 时保持静默。
                # 钩子自身的异常同样就地隔离——except 块内新异常不会被本 try 捕获，
                # 若不二次包裹会从 emit 抛穿，破坏"绝不抛穿"铁律并覆盖调用方结果。
                if self._on_error is not None:
                    try:
                        self._on_error(name, notice.phase, exc)
                    except Exception:  # 错误钩子自身异常同样隔离  # noqa: BLE001, S110
                        pass
                continue

    def subscribe(self, plugin: object) -> LifecycleBus:
        """注册 Lifecycle 插件（契约：name + register(bus)）。

        参数：
            plugin: 具名插件，实现 register(bus) 自行订阅若干相位。
        返回：
            self（支持链式）。
        异常：
            ValueError: 插件缺 name 或 register。
        """
        pname = getattr(plugin, "name", None)
        register = getattr(plugin, "register", None)
        if not pname or not callable(register):
            raise ValueError("lifecycle plugin must have name and register(bus)")
        register(self)
        return self


def runner_notifier(bus: LifecycleBus) -> Callable[[str, Mapping[str, Any]], None]:
    """把 Runner 的 notify 回调桥接到 LifecycleBus（装配层接线入口）。

    Runner 内核零依赖 hooks，只发 (相位名字符串, 只读投影)。本函数把相位名归一为
    LifecyclePhase、投影包成 Notice 发到总线——补上"9 个 Runner 相位发到 no-op
    无人接"的接线断点（规格 P2：Runner 发会话/轮次/控制流系列）。

    参数：
        bus: 目标 Lifecycle 总线。
    返回：
        可直接传给 Runner(notify=...) 的回调。未知相位名安全忽略（不抛穿内核）。
    """
    def notify(phase_name: str, projection: Mapping[str, Any]) -> None:
        try:
            phase = LifecyclePhase(phase_name)
        except ValueError:
            return  # 未知相位：安全忽略，不让通知打断主流程
        bus.emit(Notice(
            phase=phase,
            session_id=str(projection.get("session_id", "") or ""),
            port=str(projection.get("port", "") or ""),
            payload=projection,
        ))

    return notify
