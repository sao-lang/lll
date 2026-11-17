"""R1 fold：模块唯一的状态变更入口（纯函数，不可变回填）。

约定（R1 §4）：
- fold 只依赖入参，不读时间/随机/IO；
- 预算由 payload['budget'] 声明、在 fold 统一扣减（Program 不许自己数数）。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import cast

from .types import (
    AgentState, BudgetDelta, DynamicValue, Effect, EffectKind, Message,
    PortError, Result, canonical,
)

__all__ = ["field", "fold", "replay_events", "state_append_message", "state_set"]


def field(obj: DynamicValue, name: str, default: DynamicValue = None) -> DynamicValue:
    """读取对象字段，兼容 pydantic/dataclass/重放后的 Replayed(dict)。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def state_set(state: AgentState, key: str, value: DynamicValue) -> AgentState:
    """Program 用的不可变 scratch 写入（phase/cursor/steps…）。"""
    return state.set(key, value)


def state_append_message(state: AgentState, message: Message) -> AgentState:
    """向对话历史追加一条消息，返回新 State。"""
    return state.with_(messages=(*state.messages, message))


def _plain_value(obj: DynamicValue) -> DynamicValue:
    """把单个值规范化为可序列化形态。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if isinstance(obj, dict):
        return {k: _plain_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain_value(v) for v in obj]
    return obj


def _budget_delta(effect: Effect) -> BudgetDelta:
    """Effect 声明的单步预算；未声明时只扣 1 turn。"""
    declared: dict[str, DynamicValue] = dict(effect.payload.get("budget") or {})
    declared["turns"] = declared.get("turns", 1)
    return cast(BudgetDelta, declared)


def _message_for(effect: Effect, result: Result) -> Message | None:
    """按 Effect 类型生成对话消息；特殊 kind 不进 messages。"""
    data = result.data if result.ok else None
    if not result.ok:
        err = result.error or PortError("UNKNOWN_PORT_ERROR", "unknown port failure")
        return Message(role="system", content=f"[{err.code}] {err.message}")
    if effect.kind is EffectKind.LLM:
        content = field(data, "content", "") or ""
        calls = tuple(_plain_value(c) for c in (field(data, "tool_calls", []) or []))
        return Message(role="assistant", content=content, tool_calls=calls)
    if effect.kind is EffectKind.TOOL:
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
    state: AgentState, effects: Sequence[Effect], results: Sequence[Result],
    reason: str = "",
) -> AgentState:
    """把一批 Effect + Result 折叠为新 State（消息/预算/托管字段全产生新对象）。

    处理顺序：① 生成消息 ② 特殊 kind 托管字段 ③ 累计 usage ④ 统一扣预算。
    """
    messages: list[Message] = list(state.messages)
    budgets = state.budgets
    # 托管字段用显式局部变量（编译期可枚举），不用 **dict 拼装（编码规则 3）
    output: DynamicValue = state.output
    last_skill: DynamicValue = state.last_skill
    last_child: DynamicValue = state.last_child
    last_wait_human: DynamicValue = state.last_wait_human
    cost = 0.0
    tokens = 0
    for effect, result in zip(effects, results):
        message = _message_for(effect, result)
        if message is not None:
            messages.append(message)
        if effect.kind is EffectKind.SKILL:
            last_skill = _plain_value(result.data)
        elif effect.kind is EffectKind.CHILD:
            last_child = _plain_value(result.data)
        elif effect.kind is EffectKind.WAIT_HUMAN:
            last_wait_human = _plain_value(result.data)
        if effect.kind is EffectKind.END and "output" in effect.payload:
            output = _plain_value(effect.payload["output"])
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
            tokens=int(delta.get("tokens", 0)),
            parallel_children=int(delta.get("parallel_children", 0)),
        )
        cost = 0.0

    return state.with_(
        messages=tuple(messages), budgets=budgets,
        output=output, last_skill=last_skill, last_child=last_child,
        last_wait_human=last_wait_human,
        last_step_kinds=tuple(e.kind.value for e in effects),
        last_tokens=tokens,
        last_reason=reason or state.last_reason,
    )


def replay_events(events: Iterable[DynamicValue], initial_state: AgentState) -> AgentState:
    """事件溯源重放：逐步 fold 得到终态。"""
    state = initial_state
    for event in events:
        state = fold(state, event.effects, event.results, getattr(event, "reason", ""))
    return state
