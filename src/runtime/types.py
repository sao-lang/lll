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
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Generator, Mapping, Sequence

__all__ = [
    "EffectKind", "Message", "PortError", "Usage", "Result", "Budgets",
    "AgentState", "Effect", "Event", "Session", "StepReport", "RunResult",
    "RuntimeCtx", "DONE", "Program", "DispatchFn", "CallFn", "EventStore",
    "canonical", "fingerprint", "checkpoint", "restore_checkpoint",
]


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

    role: str
    content: str = ""
    name: str | None = None
    tool_calls: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "tool_calls": [_plain(tc) for tc in self.tool_calls],
        }


@dataclass(frozen=True)
class PortError:
    """结构化错误信封：{code, message, retryable}；retryable 是 M2 retry 判决依据。"""

    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Usage:
    """token 与成本元数据；免费/本地实现填 0 也是合法值。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Result:
    """Port 调用的统一信封（M3 DTO 层 re-export）。

    工具业务失败也算"成功返回"：ok=True 且 value 携带业务错误码。
    只有 Port 层异常才折叠为 ok=False + error。
    """

    ok: bool
    data: Any = None
    usage: Usage | None = None
    error: PortError | None = None

    @classmethod
    def success(cls, data: Any = None, usage: Usage | None = None) -> "Result":
        return cls(ok=True, data=data, usage=usage)

    @classmethod
    def failure(cls, code: str, message: str, retryable: bool = False) -> "Result":
        return cls(ok=False, error=PortError(code=code, message=message, retryable=retryable))


@dataclass(frozen=True)
class Budgets:
    """全局预算：上限 + 已用，由运行时强制扣减，Program 不许自己数数。"""

    max_turns: int = 30
    max_hops: int = 5
    max_rounds: int = 3
    max_cost_usd: float = 5.0
    max_parallel_children: int = 8
    used_turns: int = 0
    used_hops: int = 0
    used_rounds: int = 0
    used_cost_usd: float = 0.0
    used_parallel_children: int = 0

    def exceeded(self) -> str | None:
        """返回首个超限原因；无超限返回 None。"""
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
        return "parallel_children" if count > self.max_parallel_children else None

    def consume(
        self,
        *,
        turns: int = 0,
        hops: int = 0,
        rounds: int = 0,
        cost_usd: float = 0.0,
        parallel_children: int = 0,
    ) -> "Budgets":
        return replace(
            self,
            used_turns=self.used_turns + turns,
            used_hops=self.used_hops + hops,
            used_rounds=self.used_rounds + rounds,
            used_cost_usd=round(self.used_cost_usd + cost_usd, 10),
            used_parallel_children=self.used_parallel_children + parallel_children,
        )


def _freeze(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if isinstance(mapping, MappingProxyType):
        return mapping
    return MappingProxyType(dict(mapping or {}))


@dataclass(frozen=True)
class AgentState:
    """一次会话的全部进度；不可变对象，任何"修改"都是产生新对象。"""

    messages: tuple[Message, ...] = ()
    scratchpad: Mapping[str, Any] = field(default_factory=dict)
    budgets: Budgets = field(default_factory=Budgets)
    version: str = "0.0.0"  # program + prompt 模板版本，进 trace 供回放对齐

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "scratchpad", _freeze(self.scratchpad))

    def get(self, key: str, default: Any = None) -> Any:
        return self.scratchpad.get(key, default)

    def with_(self, **changes: Any) -> "AgentState":
        """唯一的状态变更入口（配合 fold 使用）：返回新对象，绝不原地改。"""
        if "scratchpad" in changes:
            changes["scratchpad"] = _freeze(changes["scratchpad"])
        if "messages" in changes:
            changes["messages"] = tuple(changes["messages"])
        return replace(self, **changes)

    def set(self, key: str, value: Any) -> "AgentState":
        data = dict(self.scratchpad)
        data[key] = value
        return self.with_(scratchpad=data)


@dataclass(frozen=True)
class Effect:
    """对外部世界的声明：只声明、不执行。"""

    kind: EffectKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    batch: bool = False
    trace_id: str | None = None  # 由解释器按 step 确定性注入

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EffectKind(self.kind))
        object.__setattr__(self, "payload", _freeze(self.payload))

    def with_payload(self, **extra: Any) -> "Effect":
        data = dict(self.payload)
        data.update(extra)
        return replace(self, payload=data)


class _Done:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return "DONE"


DONE = _Done()
"""Program 结束哨兵。"""

Program = Generator[Any, Any, None]
"""用户 Agent 逻辑：读 State → yield Effect → 经 send() 收回 Result → ... → return。"""


@dataclass(frozen=True)
class Event:
    """事件溯源最小单元：append-only，逻辑时钟保证可确定性回放。"""

    seq: int
    session_id: str
    step: int
    state_snapshot: AgentState
    effects: tuple[Effect, ...]
    results: tuple[Result, ...]
    batched: bool = False
    ts: int = 0  # 逻辑时钟（= step）；真实墙钟由外壳注入，绝不进纯运行时
    cost_usd: float = 0.0
    tokens: int = 0
    status: str = "running"  # running / done / suspended / error
    reason: str = ""


@dataclass
class RuntimeCtx:
    """Runner 默认构造的上下文；M2 可通过 ctx_factory 换成 ChainCtx（字段为本类超集）。"""

    effect: Effect
    state: AgentState
    session_id: str
    attempt: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    invoke: Callable[["Effect", Any], Awaitable[Result]] | None = None


@dataclass
class Session:
    """program + state + event_log 的聚合根。

    program_factory 而不是 generator 实例：checkpoint 无法序列化 generator，
    恢复时用工厂重建 generator 并重放事件日志定位到断点。
    """

    session_id: str
    program_factory: Callable[[], Program]
    state: AgentState = field(default_factory=AgentState)
    events: list[Event] = field(default_factory=list)
    version: str = "0.0.0"


@dataclass(frozen=True)
class StepReport:
    """单步报告；错误进报告而不抛穿。"""

    step: int
    state: AgentState
    effects: tuple[Effect, ...] = ()
    results: tuple[Result, ...] = ()
    status: str = "running"  # running / done / suspended / error
    reason: str = ""
    error: str | None = None
    events: tuple[Event, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """一次 run 的终态。"""

    session_id: str
    state: AgentState
    status: str
    events: tuple[Event, ...]
    output: Any = None
    reason: str = ""
    error: str | None = None


DispatchFn = Callable[["Effect", Any], Awaitable[Any]]
CallFn = Callable[["Effect", Any], Awaitable[Result]]


class EventStore:
    """checkpoint 存储协议（M7 提供真实持久化，M1 自带内存实现以独立闭环）。"""

    def save(self, session_id: str, snapshot: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def load(self, session_id: str) -> dict[str, Any] | None:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 确定性序列化：事件日志逐字节可比对、checkpoint 可持久化
# --------------------------------------------------------------------------- #
def _plain(obj: Any) -> Any:
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
        return _plain(obj.to_dict())
    return str(obj)


def canonical(obj: Any) -> str:
    """确定性 JSON：sort_keys + 紧凑分隔符，是事件哈希与快照比对的基础。"""
    return json.dumps(_plain(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(events: Sequence[Event]) -> str:
    """事件日志指纹（M1-G2 确定性门禁比对它）。"""
    return hashlib.sha256(canonical(list(events)).encode("utf-8")).hexdigest()


def checkpoint(session: Session) -> dict[str, Any]:
    """把会话折叠为可 JSON 持久化的快照（不含 program，恢复由调用方给工厂）。"""
    return {
        "session_id": session.session_id,
        "version": session.version,
        "state": _plain(session.state),
        "events": [_plain(e) for e in session.events],
        "fingerprint": fingerprint(session.events),
    }


class Replayed(dict[str, Any]):
    """重放结果代理：让 program 的 `resp.tool_calls` 等属性访问在反序列化后依然成立。"""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - 防御分支
            raise AttributeError(key) from exc


def revive(obj: Any) -> Any:
    """把规范化后的纯 dict/list 递归还原为可属性访问的对象。"""
    if isinstance(obj, dict):
        return Replayed({k: revive(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [revive(v) for v in obj]
    return obj


def restore_state(data: dict[str, Any]) -> AgentState:
    """从规范化快照还原 AgentState（保留 MappingProxyType 语义与 tuple 字段）。"""
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
        scratchpad=data["scratchpad"],
        budgets=Budgets(**data["budgets"]),
        version=data["version"],
    )


def restore_checkpoint(snapshot: dict[str, Any], program_factory: Callable[[], Program]) -> Session:
    """从快照重建 State 与事件日志；Program 纯函数 + 重放定位断点。"""
    events: list[Event] = []
    for e in snapshot["events"]:
        events.append(
            Event(
                seq=e["seq"],
                session_id=e["session_id"],
                step=e["step"],
                state_snapshot=restore_state(e["state_snapshot"]),
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
