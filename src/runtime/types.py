"""M1 公共类型层（全框架唯一自包含的类型来源）。

契约纪律（见 docs/02-模块详设/M1-核心运行时.md §4）：
- 本模块不 import 任何具体 IO / pydantic / 其他模块实现。
- M2 / M3 / M4 只允许 import 本文件；跨模块共享的信封（Result / Usage / PortError）
  定义在这里，由 M3 的 DTO 层 re-export，这样 M1 可以独立闭环测试。
- 所有"状态变更"必须产生新对象（frozen dataclass + MappingProxyType）。
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
from typing import Any, TypedDict, TypeAlias, cast

__all__ = [
    "DONE",
    "AgentState",
    "BudgetDelta",
    "Budgets",
    "CallFn",
    "DispatchFn",
    "DoneSentinel",
    "DynamicValue",
    "Effect",
    "EffectKind",
    "Event",
    "EventStore",
    "JsonSnapshot",
    "Message",
    "Payload",
    "PortError",
    "Program",
    "Result",
    "RunResult",
    "RuntimeCtx",
    "Scratchpad",
    "Session",
    "StateSnapshot",
    "StepReport",
    "Usage",
    "canonical",
    "checkpoint",
    "fingerprint",
    "restore_checkpoint",
]

# --------------------------------------------------------------------------- #
# 动态边界类型别名
#
# 运行时是「 Effect 解释器」，跨 port 的数据在编译期无法预知具体形态（由各
# port 实现决定）。这里把这些不可避免的动态值收敛成少数具名别名：既集中表达
# 「为何这里是动态的」，也让注释有明确落点，避免 Any 散落各处难以维护。
# --------------------------------------------------------------------------- #
type DynamicValue = Any
"""单个动态值：Result.data / scratchpad 条目 / 待规范化对象。其形态由 port 决定。"""

type Payload = Mapping[str, DynamicValue]
"""Effect 参数负载的只读视图：键是参数名（calls/call/budget/port/name…），
取值形态由 Effect.kind 决定；底层由 MappingProxyType 冻结，禁止原地修改。"""

type Scratchpad = Mapping[str, DynamicValue]
"""AgentState.scratchpad 的只读视图：键是进度键（phase/cursor/output/…），
与 Payload 结构相同但语义不同，故用独立别名区分；冻结只读，禁止原地修改。"""

class JsonSnapshot(TypedDict):
    """checkpoint 顶层快照：五个固定键，可 JSON 持久化。"""

    session_id: str  # 会话标识
    version: str  # 会话版本
    state: StateSnapshot  # 规范化后的 AgentState
    events: list[DynamicValue]  # 规范化后的事件日志
    fingerprint: str  # 事件日志指纹，用于确定性校验


class StateSnapshot(TypedDict):
    """AgentState 的规范化快照：键即 AgentState 字段名，由 _plain 产出。"""

    messages: list[DynamicValue]  # 对话历史
    scratchpad: dict[str, DynamicValue]  # 用户自定义键
    budgets: dict[str, DynamicValue]  # Budgets 字段
    version: str  # 状态版本
    output: DynamicValue  # END 产出
    last_skill: DynamicValue  # 最近 SKILL 结果
    last_child: DynamicValue  # 最近 CHILD 结果
    last_wait_human: DynamicValue  # 最近 WAIT_HUMAN 结果
    last_step_kinds: list[str]  # 最近一步的 kind 列表
    last_tokens: int  # 最近一步 token 数
    last_reason: str  # 最近一步迁移原因


class EffectKind(str, Enum):
    """程序对外部世界的声明种类；固定枚举，外壳按 kind 扩展 port。"""

    LLM = "llm"
    TOOL = "tool"
    CHILD = "child"
    SKILL = "skill"
    WAIT_HUMAN = "wait_human"
    END = "end"


@dataclass(frozen=True)
class Message:
    """对话历史最小单元；只增不减（裁剪属于视图层）。"""

    role: str  # 说话方标识：system / user / assistant / tool
    content: str = ""  # 文本正文；工具消息里是规范化后的 JSON 字符串
    name: str | None = None  # 可选名称；工具消息用它承载 call_id
    tool_calls: tuple[DynamicValue, ...] = ()  # LLM 返回的原始工具调用（形态由 port 决定）

    def to_dict(self) -> dict[str, DynamicValue]:
        """转为可序列化字典；工具调用逐项规范化，供 vendor payload 与快照复用。"""
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "tool_calls": [_plain(tc) for tc in self.tool_calls],
        }


@dataclass(frozen=True)
class PortError:
    """结构化错误信封：{code, message, retryable}；retryable 是 M2 retry 判决依据。"""

    code: str  # 稳定错误码，供链路按码判决（如 PORT_MISSING / PORT_CRASHED）
    message: str  # 人类可读的错误描述，进 StepReport 与 trace
    retryable: bool = False  # 是否值得重试；M2 RetryFilter 读取它做判决


@dataclass(frozen=True)
class Usage:
    """token 与成本元数据；免费/本地实现填 0 也是合法值。"""

    prompt_tokens: int = 0  # 输入 token 数
    completion_tokens: int = 0  # 输出 token 数
    cost_usd: float = 0.0  # 本步成本（美元），由适配器按定价折算


@dataclass(frozen=True)
class Result:
    """Port 调用的统一信封（M3 DTO 层 re-export）。

    工具业务失败也算"成功返回"：ok=True 且 value 携带业务错误码。
    只有 Port 层异常才折叠为 ok=False + error。
    """

    ok: bool  # 是否成功返回；业务失败也算 ok=True，只有 port 异常才是 False
    data: DynamicValue = None  # port 返回的数据，形态由具体 port 决定
    usage: Usage | None = None  # 成功时的 token / 成本元数据
    error: PortError | None = None  # 失败时的结构化错误；ok=True 时应为 None

    @classmethod
    def success(cls, data: DynamicValue = None, usage: Usage | None = None) -> Result:
        """构造成功信封：携带 data 与可选 usage，error 恒为 None。"""
        return cls(ok=True, data=data, usage=usage)

    @classmethod
    def failure(cls, code: str, message: str, retryable: bool = False) -> Result:
        """构造失败信封：折叠异常为 PortError，data / usage 恒为 None。"""
        return cls(ok=False, error=PortError(code=code, message=message, retryable=retryable))


@dataclass(frozen=True)
class Budgets:
    """全局预算：上限 + 已用，由运行时强制扣减，Program 不许自己数数。"""

    max_turns: int = 30  # 单会话最大轮数（一次 step 的 interpreter 推进算一轮）
    max_hops: int = 5  # 最大子会话递归深度（Effect(child) 链）
    max_rounds: int = 3  # 单步内最大链路重试轮数
    max_cost_usd: float = 5.0  # 最大累计成本（美元）
    max_parallel_children: int = 8  # 单步最大并行 Effect 数
    used_turns: int = 0  # 已用轮数
    used_hops: int = 0  # 已用递归深度
    used_rounds: int = 0  # 已用重试轮数
    used_cost_usd: float = 0.0  # 已用成本
    used_parallel_children: int = 0  # 本步已用并行数

    def exceeded(self) -> str | None:
        """按固定优先级检查所有硬上限，返回首个超限维度名；无超限返回 None。"""
        checks = (
            ("turns", self.used_turns >= self.max_turns),
            ("hops", self.used_hops >= self.max_hops),
            ("rounds", self.used_rounds >= self.max_rounds),
            ("cost_usd", self.used_cost_usd >= self.max_cost_usd),
            ("parallel_children", self.used_parallel_children >= self.max_parallel_children),
        )
        for name, hit in checks:
            if hit:
                return name
        return None

    def check_parallel(self, count: int) -> str | None:
        """检查并行数是否超限；超限返回维度名，否则 None（不产生副作用）。"""
        return "parallel_children" if count > self.max_parallel_children else None

    def consume(
        self,
        *,
        turns: int = 0,
        hops: int = 0,
        rounds: int = 0,
        cost_usd: float = 0.0,
        parallel_children: int = 0,
    ) -> Budgets:
        """按各维度增量生成新 Budgets（成本保留 10 位小数以避免浮点累积误差）。"""
        return replace(
            self,
            used_turns=self.used_turns + turns,
            used_hops=self.used_hops + hops,
            used_rounds=self.used_rounds + rounds,
            used_cost_usd=round(self.used_cost_usd + cost_usd, 10),
            used_parallel_children=self.used_parallel_children + parallel_children,
        )


def _freeze(mapping: Scratchpad | None) -> Scratchpad:
    """把映射冻结为只读视图；已是 MappingProxyType 时直接复用，避免重复包装。"""
    if isinstance(mapping, MappingProxyType):
        return mapping
    return MappingProxyType(dict(mapping or {}))


# get()/set()/with_() 兼容代理：旧的字符串键 → 框架托管字段名。
# 用模块级常量而非 dataclass 字段，避免改变构造签名与序列化字段集。
_MANAGED_STATE_KEYS: Mapping[str, str] = MappingProxyType({
    "output": "output",
    "last_skill": "last_skill",
    "last_child": "last_child",
    "last_wait_human": "last_wait_human",
    "last_step_kinds": "last_step_kinds",
    "last_tokens": "last_tokens",
    "last_reason": "last_reason",
})


@dataclass(frozen=True)
class AgentState:
    """一次会话的全部进度；不可变对象，任何"修改"都是产生新对象。

    字段分两层：
    - 框架托管字段（output / last_* 等）：由 fold 写入，键与类型在编译期确定，
      不再藏在动态字典里，因此有独立字段与精确类型。
    - scratchpad：仅供 program 暂存自定义键（phase / cursor / steps…），键由用户决定，
      保持开放映射。get()/set() 对旧的字符串访问做兼容代理。
    """

    messages: tuple[Message, ...] = ()  # 对话历史；append-only，裁剪交给视图层
    scratchpad: Scratchpad = field(default_factory=dict)  # 用户自定义键；冻结只读
    budgets: Budgets = field(default_factory=Budgets)  # 全局预算；由运行时强制扣减
    version: str = "0.0.0"  # program + prompt 模板版本，进 trace 供回放对齐

    # ---- 框架托管字段（原 scratchpad 里的固定键，现提升为显式字段）----
    output: DynamicValue = None  # END Effect 的最终产出；形态由 program 决定
    last_skill: DynamicValue = None  # 最近一次 SKILL 的返回正文
    last_child: DynamicValue = None  # 最近一次 CHILD 的 output
    last_wait_human: DynamicValue = None  # 最近一次 WAIT_HUMAN 的回答
    last_step_kinds: tuple[str, ...] = ()  # 最近一步的 Effect kind 列表（不可变元组）
    last_tokens: int = 0  # 最近一步累计的 token 数
    last_reason: str = ""  # 最近一步的状态迁移原因

    def __post_init__(self) -> None:
        """dataclass 构造钩子：把 messages / scratchpad / last_step_kinds 归一为不可变容器。"""
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "scratchpad", _freeze(self.scratchpad))
        object.__setattr__(self, "last_step_kinds", tuple(self.last_step_kinds))

    def get(self, key: str, default: DynamicValue = None) -> DynamicValue:
        """读取字段：框架托管键读显式字段，其余读 scratchpad；缺失返回 default。

        保留旧的字符串访问（如 state.get("output")）以兼容现有 program 与测试。
        """
        managed = _MANAGED_STATE_KEYS.get(key)
        if managed is not None:
            return getattr(self, managed)
        return self.scratchpad.get(key, default)

    def with_(self, **changes: DynamicValue) -> AgentState:
        """唯一的状态变更入口（配合 fold 使用）：返回新对象，绝不原地改。

        若变更键是框架托管键，自动改写到对应显式字段而非 scratchpad。
        """
        remapped: dict[str, DynamicValue] = {}
        for key, value in changes.items():
            managed = _MANAGED_STATE_KEYS.get(key)
            remapped[managed if managed is not None else key] = value
        if "scratchpad" in remapped:
            remapped["scratchpad"] = _freeze(remapped["scratchpad"])
        if "messages" in remapped:
            remapped["messages"] = tuple(remapped["messages"])
        if "last_step_kinds" in remapped:
            remapped["last_step_kinds"] = tuple(remapped["last_step_kinds"])
        return replace(self, **remapped)

    def set(self, key: str, value: DynamicValue) -> AgentState:
        """写入单键：框架托管键写显式字段，其余写 scratchpad；保持不可变语义。"""
        if key in _MANAGED_STATE_KEYS:
            return self.with_(**{key: value})
        data = dict(self.scratchpad)
        data[key] = value
        return self.with_(scratchpad=data)


class BudgetDelta(TypedDict, total=False):
    """Effect 声明的单步预算消耗；未提供某维度时按 0（turns 默认 1）处理。"""

    turns: int  # 消耗的轮数（默认 1）
    hops: int  # 消耗的递归深度
    rounds: int  # 消耗的重试轮数
    cost_usd: float  # 本步成本增量
    parallel_children: int  # 本步并行 Effect 数


class EffectPayload(TypedDict, total=False):
    """Effect 参数负载的已知键全集；键按 EffectKind 使用，未用到的键留空。

    total=False 让所有键可选，从而兼容异构构造（不同 kind 只填自己的键）。
    运行时它是普通 dict：自定义 port 仍可写入本类未声明的键（x-<name> 扩展点），
    但框架消费的已知键在这里获得编译期类型检查，不再靠裸字符串猜测。

    按 kind 分组：
    - LLM:      messages, budget, port, model, temperature, max_tokens, tools
    - TOOL:     call, calls, call_id, budget, port
    - END:      output, reason
    - SKILL:    name, part, port
    - CHILD:    program_factory, program, state
    - WAIT_HUMAN: question, options, port
    - 显式 port: port（并携带该 port 自定义的参数键）
    """

    messages: list[DynamicValue]  # LLM：对话消息列表
    tools: list[DynamicValue]  # LLM：工具目录
    model: str  # LLM：模型名覆盖
    temperature: float  # LLM：采样温度
    max_tokens: int  # LLM：最大输出 token
    call: DynamicValue  # TOOL：单个工具调用（ToolCall 或 dict）
    calls: list[DynamicValue]  # TOOL：批量调用列表（batch=True 时展开）
    call_id: str  # TOOL：调用关联 id
    budget: BudgetDelta  # 预算声明，由 fold 统一扣减
    port: str  # 显式 port 名（覆盖 kind 默认映射）
    output: DynamicValue  # END：最终产出
    reason: str  # END / 终端：状态迁移原因
    name: str  # SKILL：技能名
    part: str  # SKILL：披露分段（如 body）
    question: str  # WAIT_HUMAN：提问文本
    options: list[str]  # WAIT_HUMAN：候选答案
    program_factory: Callable[[], Program] | None  # CHILD：子 program 工厂
    program: Program  # CHILD：直接给定的子 program
    state: AgentState  # CHILD：子会话初始状态


def _empty_payload() -> EffectPayload:
    """Effect.payload 的 default_factory：产出空负载字典。"""
    return cast(EffectPayload, {})


@dataclass(frozen=True)
class Effect:
    """对外部世界的声明：只声明、不执行。"""

    kind: EffectKind  # 声明的外部能力种类；由 port 按 kind 分发
    payload: EffectPayload = field(default_factory=_empty_payload)  # 参数负载；冻结只读，已知键类型见 EffectPayload
    batch: bool = False  # 是否批量执行；解释器据此展开 payload["calls"]
    trace_id: str | None = None  # 由解释器按 step 确定性注入

    def __post_init__(self) -> None:
        """构造钩子：把 kind 归一为枚举、payload 冻结为只读视图。"""
        object.__setattr__(self, "kind", EffectKind(self.kind))
        object.__setattr__(self, "payload", cast(EffectPayload, _freeze(self.payload)))

    def with_payload(self, **extra: DynamicValue) -> Effect:
        """在副本上增量更新 payload，返回新 Effect（原对象不变）。"""
        data: dict[str, DynamicValue] = dict(self.payload)
        data.update(extra)
        return replace(self, payload=cast(EffectPayload, data))


class _Done:
    """结束哨兵类型（单例 DONE）；用独立类型而非 None，避免与合法 send 值混淆。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return "DONE"


DoneSentinel: TypeAlias = _Done
"""结束哨兵的类型别名，供解释器 send() 返回类型标注引用（避免暴露私有类名）。"""

DONE = _Done()
"""Program 结束哨兵。"""

Program = Generator[Effect, DynamicValue, None]
"""用户 Agent 逻辑：读 State → yield Effect → 经 send() 收回 Result → ... → return。"""


@dataclass(frozen=True)
class Event:
    """事件溯源最小单元：append-only，逻辑时钟保证可确定性回放。"""

    seq: int  # 事件序号（= 追加时的事件数），保证顺序可确定性回放
    session_id: str  # 所属会话标识
    step: int  # 产生该事件的 step 号
    state_snapshot: AgentState  # 本步 fold 后的状态快照
    effects: tuple[Effect, ...]  # 本步产出的 Effect 序列
    results: tuple[Result, ...]  # 与 effects 一一对应的 Result 序列
    batched: bool = False  # 结果是否以列表形式回填进 program
    ts: int = 0  # 逻辑时钟（= step）；真实墙钟由外壳注入，绝不进纯运行时
    cost_usd: float = 0.0  # 本步成本
    tokens: int = 0  # 本步 token 总数
    status: str = "running"  # running / done / suspended / error
    reason: str = ""  # 状态迁移原因（如 program_returned / BUDGET_EXCEEDED:turns）


@dataclass
class RuntimeCtx:
    """Runner 默认构造的上下文；M2 可通过 ctx_factory 换成 ChainCtx（字段为本类超集）。"""

    effect: Effect  # 本次 dispatch 的 Effect
    state: AgentState  # 当前状态引用（只读语义，禁止组件修改）
    session_id: str  # 会话标识
    attempt: int = 0  # 当前重试次数，由链路写入
    meta: dict[str, DynamicValue] = field(default_factory=dict)  # 组件间共享的临时元数据
    trace: list[str] = field(default_factory=list)  # 组件留名轨迹
    invoke: Callable[[Effect, DynamicValue], Awaitable[Result]] | None = None  # Runner 注入的 M3 call


@dataclass
class Session:
    """program + state + event_log 的聚合根。

    program_factory 而不是 generator 实例：checkpoint 无法序列化 generator，
    恢复时用工厂重建 generator 并重放事件日志定位到断点。
    """

    session_id: str  # 会话唯一标识
    program_factory: Callable[[], Program]  # generator 工厂；checkpoint 无法序列化 generator，恢复时重建
    state: AgentState = field(default_factory=AgentState)  # 当前状态
    events: list[Event] = field(default_factory=list)  # append-only 事件日志
    version: str = "0.0.0"  # 会话版本


@dataclass(frozen=True)
class StepReport:
    """单步报告；错误进报告而不抛穿。"""

    step: int  # step 号
    state: AgentState  # 本步结束后的状态
    effects: tuple[Effect, ...] = ()  # 本步产出的 Effect
    results: tuple[Result, ...] = ()  # 对应 Result
    status: str = "running"  # running / done / suspended / error
    reason: str = ""  # 状态迁移原因
    error: str | None = None  # 错误码（若有）
    events: tuple[Event, ...] = ()  # 截止当前的完整事件日志


@dataclass(frozen=True)
class RunResult:
    """一次 run 的终态。"""

    session_id: str  # 会话标识
    state: AgentState  # 终态
    status: str  # done / suspended / error
    events: tuple[Event, ...]  # 完整事件日志
    output: DynamicValue = None  # END Effect 写入的最终产出（原 scratchpad["output"]，已提升为字段）
    reason: str = ""  # 终态原因
    error: str | None = None  # 错误码（若有）


DispatchFn = Callable[[Effect, DynamicValue], Awaitable[DynamicValue]]
"""链路判决函数签名：接收 Effect 与上下文，产出判决结果（M2 注入）。"""

CallFn = Callable[[Effect, DynamicValue], Awaitable[Result]]
"""port 调用函数签名：接收 Effect 与上下文，产出统一的 Result 信封（M3 注入）。"""


class EventStore:
    """checkpoint 存储协议（M7 提供持久化实现，M1 内置内存默认实现以独立闭环）。"""

    def save(self, session_id: str, snapshot: JsonSnapshot) -> None:  # pragma: no cover
        """持久化会话快照；由具体存储实现覆盖。"""
        raise NotImplementedError

    def load(self, session_id: str) -> JsonSnapshot | None:  # pragma: no cover
        """读取会话快照；不存在时返回 None。由具体存储实现覆盖。"""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 确定性序列化：事件日志逐字节可比对、checkpoint 可持久化
# --------------------------------------------------------------------------- #
def _plain(obj: DynamicValue) -> DynamicValue:
    """递归规范化任意对象为纯 dict/list/标量：按 pydantic → SimpleNamespace →
    dataclass → Mapping → 序列 → to_dict 的优先级展开，兜底转为字符串。

    这是确定性序列化的核心：不依赖 dataclasses.asdict（它无法 deepcopy
    MappingProxyType），而是逐字段手工下钻。
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
        # 不能用 dataclasses.asdict：scratchpad 是 MappingProxyType，无法 deepcopy
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return _plain(obj.to_dict()) # type: ignore
    return str(obj)


def canonical(obj: DynamicValue) -> str:
    """确定性 JSON：sort_keys + 紧凑分隔符，是事件哈希与快照比对的基础。"""
    return json.dumps(_plain(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(events: Sequence[Event]) -> str:
    """事件日志指纹（M1-G2 确定性门禁比对它）。"""
    return hashlib.sha256(canonical(list(events)).encode("utf-8")).hexdigest()


def checkpoint(session: Session) -> JsonSnapshot:
    """把会话折叠为可 JSON 持久化的快照（不含 program，恢复由调用方给工厂）。"""
    return cast(JsonSnapshot, {
        "session_id": session.session_id,
        "version": session.version,
        "state": _plain(session.state),
        "events": [_plain(e) for e in session.events],
        "fingerprint": fingerprint(session.events),
    })


class Replayed(dict[str, DynamicValue]):
    """重放结果代理：让 program 的 `resp.tool_calls` 等属性访问在反序列化后依然成立。"""

    def __getattr__(self, key: str) -> DynamicValue:
        """缺失键按属性读取时抛 AttributeError（而非 KeyError），贴合对象语义。"""
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - 防御分支
            raise AttributeError(key) from exc


def revive(obj: DynamicValue) -> DynamicValue:
    """把规范化后的纯 dict/list 递归还原为可属性访问的对象。"""
    if isinstance(obj, dict):
        return Replayed({k: revive(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [revive(v) for v in obj]
    return obj


def restore_state(data: StateSnapshot) -> AgentState:
    """从规范化快照还原 AgentState（保留 MappingProxyType 语义与 tuple 字段）。

    兼容旧快照：新增的框架托管字段若顶层缺失，回退到 scratchpad 里的同名键
    （旧版本把它们存在 scratchpad），保证 checkpoint 格式升级不破坏历史数据。
    """
    scratchpad = dict(data["scratchpad"])
    raw: dict[str, DynamicValue] = dict(data)  # 转普通 dict 以兼容旧快照的动态键回退
    managed = {
        key: raw[key] if key in raw else scratchpad.pop(key, default)
        for key, default in (
            ("output", None),
            ("last_skill", None),
            ("last_child", None),
            ("last_wait_human", None),
            ("last_step_kinds", ()),
            ("last_tokens", 0),
            ("last_reason", ""),
        )
    }
    # 旧快照里 last_{kind} 也写在 scratchpad，弹出避免重复保留
    for old_key in ("last_skill", "last_child", "last_wait_human"):
        scratchpad.pop(old_key, None)
    return AgentState(
        messages=tuple(
            Message(
                role=m["role"],
                content=m["content"],
                name=m.get("name"),
                tool_calls=tuple(m.get("tool_calls") or ()),
            )
            for m in data["messages"]
        ),
        scratchpad=scratchpad,
        budgets=Budgets(**data["budgets"]),
        version=data["version"],
        **managed,
    )


def restore_checkpoint(snapshot: JsonSnapshot, program_factory: Callable[[], Program]) -> Session:
    """从快照重建 State 与事件日志；Program 纯函数 + 重放定位断点。"""
    events: list[Event] = []
    for e in snapshot["events"]:
        events.append(
            Event(
                seq=e["seq"],
                session_id=e["session_id"],
                step=e["step"],
                state_snapshot=restore_state(cast(StateSnapshot, e["state_snapshot"])),
                effects=tuple(
                    Effect(EffectKind(x["kind"]), x["payload"], x["batch"], x["trace_id"])
                    for x in e["effects"]
                ),
                results=tuple(
                    Result(
                        ok=r["ok"],
                        data=revive(r["data"]),
                        usage=Usage(**r["usage"]) if r.get("usage") else None,
                        error=PortError(**r["error"]) if r.get("error") else None,
                    )
                    for r in e["results"]
                ),
                batched=e["batched"],
                ts=e["ts"],
                cost_usd=e["cost_usd"],
                tokens=e["tokens"],
                status=e["status"],
                reason=e.get("reason", ""),
            )
        )
    return Session(
        session_id=snapshot["session_id"],
        program_factory=program_factory,
        state=restore_state(snapshot["state"]),
        events=events,
        version=snapshot["version"],
    )
