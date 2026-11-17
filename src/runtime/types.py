"""R1 核心运行时：全框架唯一自包含的类型来源。

契约纪律（见 docs/02-模块详设/Agent/R1-核心运行时.md §4）：
- 本模块不 import 任何具体 IO / pydantic / 其他模块实现。
- 上层只 import 本文件；跨模块信封（Result/Usage/PortError）定义在此。
- 所有状态变更产生新对象（frozen dataclass + MappingProxyType）。
- 墙钟不进运行时：Event.ts 用逻辑时钟（=step），真实时间由外壳注入。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import types
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, TypedDict, cast

__all__ = [
    "DONE", "AgentState", "BudgetDelta", "Budgets", "CallFn", "DispatchFn",
    "DoneSentinel", "DynamicValue", "Effect", "EffectKind", "EffectPayload", "Event",
    "EventStore", "JsonSnapshot", "Message", "Payload", "PortError", "Program",
    "Result", "RunResult", "RuntimeCtx", "Scratchpad", "Session", "StateSnapshot",
    "StepReport", "Usage", "canonical", "checkpoint", "fingerprint", "restore_checkpoint",
]

# --------------------------------------------------------------------------- #
# 动态边界：把不可避免的 Any 收敛为具名别名
# --------------------------------------------------------------------------- #
type DynamicValue = Any
"""单个动态值：Result.data / scratchpad 条目 / 待规范化对象，形态由 port 决定。"""
type Payload = Mapping[str, DynamicValue]
"""Effect 参数负载的只读视图。"""
type Scratchpad = Mapping[str, DynamicValue]
"""AgentState.scratch 只读视图；与 Payload 结构同但语义不同，故独立命名。"""


class JsonSnapshot(TypedDict):
    """checkpoint 顶层快照：五个固定键，可 JSON 持久化。

    字段：
        session_id: 会话标识。
        version: 框架/状态版本，用于迁移。
        state: 已规范化的 AgentState 快照。
        events: 已规范化的事件列表（每个事件是 dict）。
        fingerprint: 事件日志的 SHA256，防篡改/漂移。
    """
    session_id: str
    version: str
    state: StateSnapshot
    events: list[DynamicValue]
    fingerprint: str


class StateSnapshot(TypedDict):
    """AgentState 的规范化快照：键即 AgentState 字段名。

    字段：
        messages: 规范化后的消息列表。
        scratch: 用户自定义键值命名空间。
        budgets: 规范化后的预算字典。
        version: 状态版本。
        output: 最终输出。
        last_skill / last_child / last_wait_human: 最近一次特殊 kind 的结果。
        last_step_kinds: 最近一步的 Effect kind 序列。
        last_tokens: 最近一步累计 token。
        last_reason: 最近一次结束/挂起原因。
    """
    messages: list[DynamicValue]
    scratch: dict[str, DynamicValue]
    budgets: dict[str, DynamicValue]
    version: str
    output: DynamicValue
    last_skill: DynamicValue
    last_child: DynamicValue
    last_wait_human: DynamicValue
    last_step_kinds: list[str]
    last_tokens: int
    last_reason: str


class EffectKind(str, Enum):
    """程序对外部世界的声明种类；固定枚举，外壳按 kind 扩展 port。

    成员：
        LLM: 大模型补全。TOOL: 工具调用。CHILD: 子 Agent。
        SKILL: 技能加载。WAIT_HUMAN: 挂起等人。END: 结束并产出 output。
    """
    LLM = "llm"
    TOOL = "tool"
    CHILD = "child"
    SKILL = "skill"
    WAIT_HUMAN = "wait_human"
    END = "end"


@dataclass(frozen=True)
class Message:
    """对话历史最小单元；只增不减（裁剪属于视图层，R7）。

    字段：
        role: 角色（system/user/assistant/tool）。
        content: 正文；tool 消息里放规范化结果的 canonical JSON。
        name: 可选名称（tool 消息里存 call_id）。
        tool_calls: 助手消息携带的工具调用（形态由模型决定）。
    """

    role: str
    content: str = ""
    name: str | None = None
    tool_calls: tuple[DynamicValue, ...] = ()

    def to_dict(self) -> dict[str, DynamicValue]:
        """转为可序列化字典，供 vendor payload 与快照复用。

        返回：
            role/content/name/tool_calls 四键字典；tool_calls 逐项规范化。
        """
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "tool_calls": [_plain(tc) for tc in self.tool_calls],
        }


@dataclass(frozen=True)
class PortError:
    """结构化错误信封：{code, message, retryable}；retryable 驱动 R3 重试。

    字段：
        code: 稳定错误码。message: 人类可读信息。
        retryable: 是否可重试（R3 据此决策，R1 只透传不解释）。
    """
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Usage:
    """token 与成本元数据；本地/免费实现填 0 也合法。

    字段：
        prompt_tokens: 输入 token。completion_tokens: 输出 token。
        cost_usd: 本步成本（美元）。
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Result:
    """Port 调用统一信封；业务失败也算 ok=True，仅 Port 异常为 ok=False。

    字段：
        ok: Port 是否正常返回（不含业务成败判断）。
        data: 返回数据，形态由 Port 决定。
        usage: token/成本元数据。
        error: 仅 ok=False 时存在的结构化错误。
    """
    ok: bool
    data: DynamicValue = None
    usage: Usage | None = None
    error: PortError | None = None

    @classmethod
    def success(cls, data: DynamicValue = None, usage: Usage | None = None) -> Result:
        """构造成功信封。

        参数：
            data: 成功数据。usage: 可选用量。
        """
        return cls(ok=True, data=data, usage=usage)

    @classmethod
    def failure(cls, code: str, message: str, retryable: bool = False) -> Result:
        """构造失败信封，折叠异常为 PortError。

        参数：
            code: 稳定错误码。message: 错误信息。
            retryable: 是否可重试（交 R3 决策）。
        """
        return cls(ok=False, error=PortError(code=code, message=message, retryable=retryable))


@dataclass(frozen=True)
class Budgets:
    """多维预算：上限 + 已用，由 R1 强制扣减，Program 不许自己数数。

    字段（每项都有 max_* 上限与 used_* 已用）：
        turns: 步数。hops: 子 Agent 深度。rounds: 轮次。
        cost_usd: 成本。tokens: token 数。parallel_children: 并行子 Agent 数。
    """
    max_turns: int = 30
    max_hops: int = 5
    max_rounds: int = 3
    max_cost_usd: float = 5.0
    max_tokens: int = 100_000
    max_parallel_children: int = 8
    used_turns: int = 0
    used_hops: int = 0
    used_rounds: int = 0
    used_cost_usd: float = 0.0
    used_tokens: int = 0
    used_parallel_children: int = 0

    def exceeded(self) -> str | None:
        """按固定优先级检查所有硬上限，返回首个超限维度名。

        返回：
            维度名（如 "turns"），全部未超限返回 None。
        说明：
            checks 元组顺序即优先级；命中即返回，保证多维护约时错误码确定。
        """
        checks = (
            ("turns", self.used_turns >= self.max_turns),
            ("hops", self.used_hops >= self.max_hops),
            ("rounds", self.used_rounds >= self.max_rounds),
            ("cost_usd", self.used_cost_usd >= self.max_cost_usd),
            ("tokens", self.used_tokens >= self.max_tokens),
            ("parallel_children", self.used_parallel_children >= self.max_parallel_children),
        )
        for name, hit in checks:
            if hit:
                return name
        return None

    def check_parallel(self, count: int) -> str | None:
        """检查并行数是否超限；不产生副作用。

        参数：
            count: 本批准备并发的 Effect 数。
        返回：
            超限返回 "parallel_children"，否则 None。
        """
        return "parallel_children" if count > self.max_parallel_children else None

    def consume(
        self, *, turns: int = 0, hops: int = 0, rounds: int = 0,
        cost_usd: float = 0.0, tokens: int = 0, parallel_children: int = 0,
    ) -> Budgets:
        """按各维度增量生成新 Budgets（成本 round 到 10 位避免浮点累积）。

        参数：
            turns/hops/rounds/tokens/parallel_children: 对应维度增量。
            cost_usd: 成本增量（round 到 10 位，防止多次累加的浮点误差）。
        返回：
            已累加的新 Budgets（原对象不变）。
        """
        return replace(
            self,
            used_turns=self.used_turns + turns,
            used_hops=self.used_hops + hops,
            used_rounds=self.used_rounds + rounds,
            used_cost_usd=round(self.used_cost_usd + cost_usd, 10),
            used_tokens=self.used_tokens + tokens,
            used_parallel_children=self.used_parallel_children + parallel_children,
        )


def _freeze(mapping: Scratchpad | None) -> Scratchpad:
    """把映射冻结为只读视图；已是 MappingProxyType 时复用（避免重复包装）。"""
    if isinstance(mapping, MappingProxyType):
        return mapping
    return MappingProxyType(dict(mapping or {}))


_MANAGED_SCRATCH_KEYS: Mapping[str, str] = MappingProxyType({
    # 用户代码历史上用 scratch 读托管键，这里把旧键映射到 AgentState 的具名字段。
    "output": "output", "last_skill": "last_skill", "last_child": "last_child",
    "last_wait_human": "last_wait_human", "last_step_kinds": "last_step_kinds",
    "last_tokens": "last_tokens", "last_reason": "last_reason",
})


@dataclass(frozen=True)
class AgentState:
    """一次会话全部进度；不可变，任何修改产生新对象。

    字段分两层：框架托管字段（编译期确定类型，由 fold 写入）+ scratch（用户自定义键）。

    字段：
        messages: 对话历史（只增）。
        scratch: 用户自定义只读命名空间（键由用户决定，故动态）。
        budgets: 多维预算（上限 + 已用）。
        version: 状态版本。
        output: 最终输出（END 写入）。
        last_skill / last_child / last_wait_human: 最近一次特殊 kind 的结果。
        last_step_kinds: 最近一步的 Effect kind 序列。
        last_tokens: 最近一步累计 token。
        last_reason: 最近一次结束/挂起原因。
    """
    messages: tuple[Message, ...] = ()
    scratch: Scratchpad = field(default_factory=dict)
    budgets: Budgets = field(default_factory=Budgets)
    version: str = "0.0.0"
    output: DynamicValue = None
    last_skill: DynamicValue = None
    last_child: DynamicValue = None
    last_wait_human: DynamicValue = None
    last_step_kinds: tuple[str, ...] = ()
    last_tokens: int = 0
    last_reason: str = ""

    def __post_init__(self) -> None:
        """归一 messages / scratch / last_step_kinds 为不可变容器（frozen 下用 object.__setattr__）。"""
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "scratch", _freeze(self.scratch))
        object.__setattr__(self, "last_step_kinds", tuple(self.last_step_kinds))

    def get(self, key: str, default: DynamicValue = None) -> DynamicValue:
        """统一读：框架托管键读显式字段，其余读 scratch。

        参数：
            key: 键名。default: scratch 缺失时的回退值。
        返回：
            键值（托管字段优先）。
        """
        managed = _MANAGED_SCRATCH_KEYS.get(key)
        if managed is not None:
            return getattr(self, managed)
        return self.scratch.get(key, default)

    def set(self, key: str, value: DynamicValue) -> AgentState:
        """写入单键，返回新对象（不可变语义）。

        托管字段（编译期可枚举）走显式分支；其余键写入用户 scratch（动态命名空间）。

        参数：
            key: 键名。value: 写入值。
        返回：
            写入后的新 AgentState。
        """
        if key in _MANAGED_SCRATCH_KEYS:
            # 显式分派到具名字段，不用 **{key: value} 动态拼装（编码规则 3）
            return self.with_(
                output=value if key == "output" else self.output,
                last_skill=value if key == "last_skill" else self.last_skill,
                last_child=value if key == "last_child" else self.last_child,
                last_wait_human=value if key == "last_wait_human" else self.last_wait_human,
                last_step_kinds=value if key == "last_step_kinds" else self.last_step_kinds,
                last_tokens=value if key == "last_tokens" else self.last_tokens,
                last_reason=value if key == "last_reason" else self.last_reason,
            )
        data = dict(self.scratch)  # 用户键：复制 scratch 后写入（动态命名空间）
        data[key] = value
        return self.with_(scratch=data)

    def with_(self, **changes: DynamicValue) -> AgentState:
        """唯一状态变更入口：返回新对象，托管键改写到显式字段。

        参数：
            **changes: 要更新的字段；旧托管键名会 remap 到具名字段。
        返回：
            应用变更后的新 AgentState。
        说明：
            scratch/messages/last_step_kinds 在 replace 前先归一为不可变容器。
        """
        remapped: dict[str, DynamicValue] = {}
        for key, value in changes.items():
            managed = _MANAGED_SCRATCH_KEYS.get(key)
            remapped[managed if managed is not None else key] = value
        if "scratch" in remapped:
            remapped["scratch"] = _freeze(remapped["scratch"])
        if "messages" in remapped:
            remapped["messages"] = tuple(remapped["messages"])
        if "last_step_kinds" in remapped:
            remapped["last_step_kinds"] = tuple(remapped["last_step_kinds"])
        return replace(self, **remapped)


class BudgetDelta(TypedDict, total=False):
    """Effect 声明的单步预算消耗；未提供某维度按 0（turns 默认 1）。

    字段：
        turns/hops/rounds/tokens/parallel_children: 各维度整数增量。
        cost_usd: 成本增量。
    """
    turns: int
    hops: int
    rounds: int
    cost_usd: float
    tokens: int
    parallel_children: int


class EffectPayload(TypedDict, total=False):
    """Effect 参数负载的已知键全集（total=False 兼容异构构造）。

    字段（按 Effect kind 选用）：
        messages/tools/model/temperature/max_tokens: LLM 请求参数。
        call/calls/call_id: 工具调用（单值/批量/关联 id）。
        budget: 本步预算声明。port: 目标 port 名。
        output/reason: END 的输出与原因。name/part/question/options: 技能/HITL 参数。
        program_factory/program/state: CHILD 子 Agent 的构造参数。
    """
    messages: list[DynamicValue]
    tools: list[DynamicValue]
    model: str
    temperature: float
    max_tokens: int
    call: DynamicValue
    calls: list[DynamicValue]
    call_id: str
    budget: BudgetDelta
    port: str
    output: DynamicValue
    reason: str
    name: str
    part: str
    question: str
    options: list[str]
    program_factory: Callable[[], Program] | None
    program: Program
    state: AgentState


def _empty_payload() -> EffectPayload:
    """Effect.payload 的 default_factory（返回空 EffectPayload）。"""
    return cast(EffectPayload, {})


@dataclass(frozen=True)
class Effect:
    """对外部世界的声明：只声明、不执行。

    字段：
        kind: Effect 种类（决定哪个 port 执行）。
        payload: 参数负载（已知键在 EffectPayload 声明，x-* 扩展键动态）。
        batch: 是否批量（payload['calls'] 会被解释器展开）。
        trace_id: 链路标识，由 Runner 按 step/index 注入。
    """
    kind: EffectKind
    payload: EffectPayload = field(default_factory=_empty_payload)
    batch: bool = False
    trace_id: str | None = None

    def __post_init__(self) -> None:
        """归一 kind 为枚举、payload 冻结为只读视图（frozen 下用 object.__setattr__）。"""
        object.__setattr__(self, "kind", EffectKind(self.kind))
        object.__setattr__(self, "payload", cast(EffectPayload, _freeze(self.payload)))

    def with_payload(self, **extra: DynamicValue) -> Effect:
        """在副本上增量更新 payload，返回新 Effect（原对象不变）。

        参数：
            **extra: 要合并进 payload 的键值对（已知键 + x-* 扩展键）。
        返回：
            payload 更新后的新 Effect。
        """
        data: dict[str, DynamicValue] = dict(self.payload)
        data.update(extra)
        return replace(self, payload=cast(EffectPayload, data))


class _Done:
    """Program 结束哨兵类型（单例 DONE）。"""
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        """返回 `DONE`，便于调试时识别结束哨兵。"""
        return "DONE"


type DoneSentinel = _Done  # PEP 695（Python ≥3.12）
DONE = _Done()
Program = Generator[Effect, DynamicValue, None]
"""用户 Agent 逻辑：读 State → yield Effect → send() 收结果 → ... → return。"""


@dataclass(frozen=True)
class Event:
    """事件溯源最小单元：append-only，逻辑时钟保证确定性回放。

    字段：
        seq: 事件序号。session_id: 会话标识。step: 逻辑步号。
        state_snapshot: 该步折叠后的 State 快照。
        effects / results: 该步的 Effect 与 Result（按位置对应）。
        batched: 是否批量执行。ts: 逻辑时钟（=step）。
        cost_usd / tokens: 该步用量。
        status: running/done/suspended/error。reason: 结束/挂起原因。
    """
    seq: int
    session_id: str
    step: int
    state_snapshot: AgentState
    effects: tuple[Effect, ...]
    results: tuple[Result, ...]
    batched: bool = False
    ts: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    status: str = "running"
    reason: str = ""


@dataclass
class RuntimeCtx:
    """Runner 默认构造的上下文；上层可换成字段超集（R2 的 HookCtx）。

    字段：
        effect: 当前 Effect。state: 当前状态。session_id: 会话标识。
        attempt: 尝试次数。meta: 临时元数据。trace: hook/执行轨迹。
        invoke: 由 Runner 注入的 Port 调用回调（洋葱中心使用）。
    """
    effect: Effect
    state: AgentState
    session_id: str
    attempt: int = 0
    meta: dict[str, DynamicValue] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    invoke: Callable[[Effect, DynamicValue], Awaitable[Result]] | None = None


@dataclass
class Session:
    """program + state + event_log 聚合根。

    program_factory 而非 generator：checkpoint 不可序列化，恢复时重建并重放。

    字段：
        session_id: 会话标识。
        program_factory: 重建 Program 的无参工厂。
        state: 当前状态。events: 事件日志（append-only）。
        version: 会话版本。
    """
    session_id: str
    program_factory: Callable[[], Program]
    state: AgentState = field(default_factory=AgentState)
    events: list[Event] = field(default_factory=list)
    version: str = "0.0.0"


@dataclass(frozen=True)
class StepReport:
    """单步报告；错误进报告不抛穿。

    字段：
        step: 步号。state: 折叠后状态。
        effects / results: 本步 Effect/Result。
        status / reason / error: 本步状态、原因、错误码。
        events: 截至本步的全部事件。
    """
    step: int
    state: AgentState
    effects: tuple[Effect, ...] = ()
    results: tuple[Result, ...] = ()
    status: str = "running"
    reason: str = ""
    error: str | None = None
    events: tuple[Event, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """一次 run 的终态。

    字段：
        session_id: 会话标识。state: 终态状态。
        status: done/suspended/error。events: 全部事件。
        output: 最终输出。reason / error: 结束原因与错误码。
    """
    session_id: str
    state: AgentState
    status: str
    events: tuple[Event, ...]
    output: DynamicValue = None
    reason: str = ""
    error: str | None = None


DispatchFn = Callable[[Effect, DynamicValue], Awaitable[DynamicValue]]
"""链路判决函数签名（R2 注入）；返回 Decision / Result / None。"""
CallFn = Callable[[Effect, DynamicValue], Awaitable[Result]]
"""port 调用函数签名（上层注入）；返回统一 Result 信封。"""


class EventStore:
    """checkpoint 存储协议；上层提供持久化实现，R1 内置内存默认实现以独立闭环。"""

    def save(self, session_id: str, snapshot: JsonSnapshot) -> None:  # pragma: no cover
        """持久化会话快照。

        参数：
            session_id: 会话标识。snapshot: 快照信封。
        """
        raise NotImplementedError

    def load(self, session_id: str) -> JsonSnapshot | None:  # pragma: no cover
        """读取会话快照；不存在返回 None。"""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 确定性序列化：事件日志逐字节可比对、checkpoint 可持久化
# --------------------------------------------------------------------------- #
def _plain(obj: DynamicValue) -> DynamicValue:
    """递归规范化任意对象为纯 dict/list/标量（确定性序列化核心）。

    分支按优先级：
        标量(None/str/int/float/bool) → Enum.value → pydantic.model_dump →
        SimpleNamespace.vars → dataclass 字段 → 类对象转 str → Mapping →
        序列(list/tuple/set/frozenset) → to_dict() → 兜底 str(obj)。
    说明：
        set/frozenset 转为 list 时顺序不确定；本函数主要用于已排序的快照结构，
        若未来需对 set 直接做哈希，应先排序元素。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "model_dump"):
        return _plain(obj.model_dump(mode="python"))
    if isinstance(obj, types.SimpleNamespace):
        return {k: _plain(v) for k, v in vars(obj).items()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    # 类对象（含 dataclass 类本身）按普通值处理。提前排除可消除 pyright 对
    # is_dataclass() 的 TypeGuard 收窄结果 type[DataclassInstance]，避免其在
    # 后续 hasattr/to_dict 分支误报"类对象无 to_dict 属性"。运行时行为不变。
    if isinstance(obj, type):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return _plain(getattr(obj, "to_dict")())
    return str(obj)  # 兜底：任何未知对象按字符串，保证最终可 JSON 序列化


def canonical(obj: DynamicValue) -> str:
    """确定性 JSON：sort_keys + 紧凑分隔符，事件哈希与快照比对的基础。

    参数：
        obj: 任意对象（先经 _plain 规范化）。
    返回：
        排序、紧凑、保留中文的 JSON 字符串。
    """
    return json.dumps(_plain(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(events: Sequence[Event]) -> str:
    """事件日志指纹（确定性门禁比对它）；对规范化列表取 SHA256。"""
    return hashlib.sha256(canonical(list(events)).encode("utf-8")).hexdigest()


def checkpoint(session: Session) -> JsonSnapshot:
    """把会话折叠为可 JSON 持久化的快照（不含 program，因其不可序列化）。

    参数：
        session: 目标会话。
    返回：
        JsonSnapshot：state/events 均经规范化，附事件指纹。
    """
    return cast(JsonSnapshot, {
        "session_id": session.session_id,
        "version": session.version,
        "state": _plain(session.state),
        "events": [_plain(e) for e in session.events],
        "fingerprint": fingerprint(session.events),
    })


class Replayed(dict[str, DynamicValue]):
    """重放结果代理：让 program 的 resp.tool_calls 等属性访问在反序列化后成立。"""

    def __getattr__(self, key: str) -> DynamicValue:
        """把字典键当属性访问；缺失键抛 AttributeError（符合属性访问语义）。"""
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(key) from exc


def revive(obj: DynamicValue) -> DynamicValue:
    """把规范化后的纯 dict/list 递归还原为可属性访问的对象。

    分支：
        dict → Replayed（键可当属性访问）；list → 逐项 revive；其余标量原样。
    """
    if isinstance(obj, dict):
        return Replayed({k: revive(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [revive(v) for v in obj]
    return obj


def restore_state(data: StateSnapshot) -> AgentState:
    """从规范化快照还原 AgentState（兼容旧快照：缺字段回退 scratch）。

    参数：
        data: 规范化状态快照。
    返回：
        还原的 AgentState。
    说明：
        旧快照把托管字段塞在 scratch；managed() 优先读显式键，缺失才从 scratch 弹出。
    """
    scratchpad = dict(data["scratch"])
    raw: dict[str, DynamicValue] = dict(data)  # TypedDict → 普通 dict，支持变量键读取

    def managed(key: str, default: DynamicValue) -> DynamicValue:
        """读托管字段：优先显式字段，兼容旧快照从 scratch 回退。"""
        return raw[key] if key in raw else scratchpad.pop(key, default)

    output = managed("output", None)
    last_skill = managed("last_skill", None)
    last_child = managed("last_child", None)
    last_wait_human = managed("last_wait_human", None)
    last_step_kinds = managed("last_step_kinds", ())
    last_tokens = managed("last_tokens", 0)
    last_reason = managed("last_reason", "")
    # 旧快照里这些键可能仍留在 scratch：弹出避免与托管字段重复。
    for old_key in ("last_skill", "last_child", "last_wait_human"):
        scratchpad.pop(old_key, None)
    return AgentState(
        messages=tuple(
            Message(role=m["role"], content=m["content"], name=m.get("name"),
                    tool_calls=tuple(m.get("tool_calls") or ()))
            for m in data["messages"]
        ),
        scratch=scratchpad,
        budgets=Budgets(**data["budgets"]),
        version=data["version"],
        output=output, last_skill=last_skill, last_child=last_child,
        last_wait_human=last_wait_human, last_step_kinds=last_step_kinds,
        last_tokens=last_tokens, last_reason=last_reason,
    )


def restore_checkpoint(snapshot: JsonSnapshot, program_factory: Callable[[], Program]) -> Session:
    """从快照重建 State 与事件日志；Program 纯函数 + 重放定位断点。

    参数：
        snapshot: checkpoint 快照。
        program_factory: 重建 Program 的无参工厂（快照不含 generator）。
    返回：
        恢复的 Session（State + 事件日志）。
    """
    events: list[Event] = []
    for e in snapshot["events"]:  # 逐事件重建，字段一一回填
        events.append(
            Event(
                seq=e["seq"], session_id=e["session_id"], step=e["step"],
                state_snapshot=restore_state(cast(StateSnapshot, e["state_snapshot"])),
                effects=tuple(
                    Effect(EffectKind(x["kind"]), x["payload"], x["batch"], x["trace_id"])
                    for x in e["effects"]
                ),
                results=tuple(
                    Result(ok=r["ok"], data=revive(r["data"]),
                           usage=Usage(**r["usage"]) if r.get("usage") else None,
                           error=PortError(**r["error"]) if r.get("error") else None)
                    for r in e["results"]
                ),
                batched=e["batched"], ts=e["ts"], cost_usd=e["cost_usd"],
                tokens=e["tokens"], status=e["status"], reason=e.get("reason", ""),
            )
        )
    return Session(
        session_id=snapshot["session_id"], program_factory=program_factory,
        state=restore_state(snapshot["state"]), events=events, version=snapshot["version"],
    )
