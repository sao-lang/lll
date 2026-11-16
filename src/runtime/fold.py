"""fold：模块唯一的状态变更入口（纯函数，不可变回填）。

约定（M1 §5）：
- fold(state, effects, results) 只依赖入参，不读时间 / 随机 / IO；
- Program 内不直接改 messages / scratchpad，用本文件的 helper 产生新 State；
- 预算由 payload["budget"] 声明、运行时在 fold 里统一扣减（Program 不许自己数数）。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from .types import AgentState, Effect, EffectKind, Message, PortError, Result, canonical

__all__ = [
    "fold", "field", "state_set", "state_append_message", "fold_tool_calls",
    "fold_tool_results", "replay_events",
]


def field(obj: Any, name: str, default: Any = None) -> Any:
    """同时兼容 pydantic 对象、dataclass 与重放后的 Replayed(dict)。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def state_set(state: AgentState, key: str, value: Any) -> AgentState:
    """Program 用的不可变 scratchpad 写入（phase / steps / cursor / hops 等）。"""
    return state.set(key, value)


def state_append_message(state: AgentState, message: Message) -> AgentState:
    return state.with_(messages=(*state.messages, message))


def fold_tool_calls(state: AgentState, calls: Iterable[Any]) -> AgentState:
    """ReAct 用：把本轮模型给出的 tool_calls 记进 scratchpad（纯函数）。"""
    return state.set("pending_tool_calls", [_plain_value(c) for c in calls])


def fold_tool_results(state: AgentState, results: Iterable[Any]) -> AgentState:
    """ReAct 用：把工具结果记进 scratchpad（纯函数）。"""
    return state.set("last_tool_results", [_plain_value(r) for r in results])


def _plain_value(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if isinstance(obj, dict):
        return {k: _plain_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain_value(v) for v in obj]
    return obj


def _budget_delta(effect: Effect) -> dict[str, Any]:
    """Effect 可声明本步消耗的预算；未声明时只扣 1 turn。"""
    declared = dict(effect.payload.get("budget") or {})
    declared["turns"] = declared.get("turns", 1)
    return declared


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
        call = effect.payload.get("call") or {}
        call_id = field(call, "call_id", effect.payload.get("call_id", ""))
        payload = {"ok": field(data, "ok", True), "value": _plain_value(field(data, "value")),
                   "error": field(data, "error")}
        return Message(role="tool", content=canonical(payload), name=str(call_id))
    return None


def fold(
    state: AgentState,
    effects: Sequence[Effect],
    results: Sequence[Result],
    reason: str = "",
) -> AgentState:
    """把一批 Effect + Result 折叠为新 State：messages / scratchpad / budgets 全部产生新对象。"""
    messages: list[Message] = list(state.messages)
    scratchpad = dict(state.scratchpad)
    budgets = state.budgets

    cost = 0.0
    tokens = 0
    for effect, result in zip(effects, results):
        message = _message_for(effect, result)
        if message is not None:
            messages.append(message)
        if effect.kind in (EffectKind.SKILL, EffectKind.CHILD, EffectKind.WAIT_HUMAN):
            scratchpad[f"last_{effect.kind.value}"] = _plain_value(result.data)
        if effect.kind is EffectKind.END:
            if "output" in effect.payload:
                scratchpad["output"] = _plain_value(effect.payload["output"])
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

    scratchpad["last_step_kinds"] = [e.kind.value for e in effects]
    scratchpad["last_tokens"] = tokens
    if reason:
        scratchpad["last_reason"] = reason
    return state.with_(messages=tuple(messages), scratchpad=scratchpad, budgets=budgets)


def replay_events(events: Iterable[Any], initial_state: AgentState) -> AgentState:
    """事件溯源重放：逐步 fold，得到终态（M1-T03 与终态断言全等）。"""
    state = initial_state
    for event in events:
        state = fold(state, event.effects, event.results, getattr(event, "reason", ""))
    return state
