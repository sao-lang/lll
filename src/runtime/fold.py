"""fold：模块唯一的状态变更入口（纯函数，不可变回填）。

约定（M1 §5）：
- fold(state, effects, results) 只依赖入参，不读时间 / 随机 / IO；
- Program 内不直接改 messages / scratchpad，用本文件的 helper 产生新 State；
- 预算由 payload["budget"] 声明、运行时在 fold 里统一扣减（Program 不许自己数数）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import cast

from .types import (
    AgentState,
    BudgetDelta,
    DynamicValue,
    Effect,
    EffectKind,
    Message,
    PortError,
    Result,
    canonical,
)

__all__ = [
    "field",
    "fold",
    "replay_events",
    "state_append_message",
    "state_set",
]


def field(obj: DynamicValue, name: str, default: DynamicValue = None) -> DynamicValue:
    """读取对象字段，同时兼容 pydantic 对象、dataclass 与重放后的 Replayed(dict)。

    这是运行时与「数据形态未知」之间的缓冲层：port 返回的 data 可能是任意
    对象或字典，本函数用统一接口取值，屏蔽二者差异。obj 为 None 时直接回退 default。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def state_set(state: AgentState, key: str, value: DynamicValue) -> AgentState:
    """Program 用的不可变 scratchpad 写入（phase / steps / cursor / hops 等）。"""
    return state.set(key, value)


def state_append_message(state: AgentState, message: Message) -> AgentState:
    """向对话历史追加一条消息，返回新 State（原 State 不变）。"""
    return state.with_(messages=(*state.messages, message))


def _plain_value(obj: DynamicValue) -> DynamicValue:
    """把单个值规范化为可序列化形态（pydantic model_dump + 递归 dict/list）。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if isinstance(obj, dict):
        return {k: _plain_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain_value(v) for v in obj]
    return obj


def _budget_delta(effect: Effect) -> BudgetDelta:
    """Effect 可声明本步消耗的预算；未声明时只扣 1 turn。"""
    declared: dict[str, DynamicValue] = dict(effect.payload.get("budget") or {})
    declared["turns"] = declared.get("turns", 1)
    return cast(BudgetDelta, declared)


def _message_for(effect: Effect, result: Result) -> Message | None:
    data = result.data if result.ok else None
    if not result.ok:
        err = result.error or PortError("UNKNOWN_PORT_ERROR", "unknown port failure")
        return Message(role="system", content=f"[{err.code}] {err.message}")
    if effect.kind is EffectKind.LLM:
        content = field(data, "content", "") or ""
        calls = tuple(_plain_value(c) for c in (field(data, "tool_calls", []) or []))
        return Message(role="assistant", content=content, tool_calls=calls)
    if effect.kind is EffectKind.TOOL:
        # 工具消息：把 ok/value/error 规范化成 JSON 字符串，name 用 call_id 关联调用
        call = effect.payload.get("call") or {}
        call_id = field(call, "call_id", effect.payload.get("call_id", ""))
        payload = {
            "ok": field(data, "ok", True),
            "value": _plain_value(field(data, "value")),
            "error": field(data, "error"),
        }
        return Message(role="tool", content=canonical(payload), name=str(call_id))
    return None


def fold(
    state: AgentState,
    effects: Sequence[Effect],
    results: Sequence[Result],
    reason: str = "",
) -> AgentState:
    """把一批 Effect + Result 折叠为新 State：messages / scratchpad / budgets 全部产生新对象。

    处理顺序（逐 Effect/Result 对）：① 生成对话消息 ② 特殊 kind 的副产物写入
    框架托管字段 ③ 累计 usage 成本/token ④ 统一扣减预算。reason 写入
    last_reason 字段，供链路读取。scratchpad（用户自定义键）原样透传。
    """
    messages: list[Message] = list(state.messages)
    budgets = state.budgets

    # 框架托管字段的工作副本；同名 effect 多次出现时以最后一个为准（与原覆盖语义一致）
    managed: dict[str, DynamicValue] = {}
    cost = 0.0
    tokens = 0
    for effect, result in zip(effects, results):
        message = _message_for(effect, result)
        if message is not None:
            messages.append(message)
        if effect.kind in (EffectKind.SKILL, EffectKind.CHILD, EffectKind.WAIT_HUMAN):
            managed[f"last_{effect.kind.value}"] = _plain_value(result.data)
        if effect.kind is EffectKind.END and "output" in effect.payload:
            managed["output"] = _plain_value(effect.payload["output"])
        if result.usage is not None:
            cost += float(field(result.usage, "cost_usd", 0.0))
            tokens += int(field(result.usage, "prompt_tokens", 0)) + int(
                field(result.usage, "completion_tokens", 0)
            )
        delta = _budget_delta(effect)
        budgets = budgets.consume(
            turns=int(delta.get("turns", 0)),
            hops=int(delta.get("hops", 0)),
            rounds=int(delta.get("rounds", 0)),
            cost_usd=float(delta.get("cost_usd", 0.0)) + cost,
            parallel_children=int(delta.get("parallel_children", 0)),
        )
        cost = 0.0  # 已计入本 step，避免重复累加

    managed["last_step_kinds"] = tuple(e.kind.value for e in effects)
    managed["last_tokens"] = tokens
    if reason:
        managed["last_reason"] = reason
    # with_ 会把托管键改写到显式字段，scratchpad 不再承载这些固定键
    return state.with_(messages=tuple(messages), budgets=budgets, **managed)


def replay_events(events: Iterable[DynamicValue], initial_state: AgentState) -> AgentState:
    """事件溯源重放：逐步 fold，得到终态（M1-T03 与终态断言全等）。"""
    state = initial_state
    for event in events:
        state = fold(state, event.effects, event.results, getattr(event, "reason", ""))
    return state
