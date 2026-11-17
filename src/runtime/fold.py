"""R1 fold：模块唯一的状态变更入口（纯函数，不可变回填）。

约定（R1 §4）：
- fold 只依赖入参，不读时间/随机/IO；同样的入参必得同样的 State。
- 预算由 payload['budget'] 声明、在 fold 统一扣减（Program 不许自己数数）。
- 所有返回都是新对象（frozen dataclass + with_），绝不就地修改入参 State。
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
    """读取对象字段，兼容三种形态：pydantic 模型 / dict / dataclass / Replayed。

    参数：
        obj: 待读取对象；None 直接返回 default。
        name: 字段名。
        default: 字段缺失时的回退值。

    返回：
        字段值，形态由外部对象决定（DynamicValue）。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):  # Replayed / 纯 dict：按键读
        return obj.get(name, default)
    return getattr(obj, name, default)  # pydantic / dataclass：按属性读


def state_set(state: AgentState, key: str, value: DynamicValue) -> AgentState:
    """Program 用的不可变 scratch 写入（phase/cursor/steps…），返回新 State。

    参数：
        state: 当前状态（不变）。
        key: 写入键；托管键由 AgentState.set 内部显式分派。
        value: 写入值。
    """
    return state.set(key, value)


def state_append_message(state: AgentState, message: Message) -> AgentState:
    """向对话历史追加一条消息，返回新 State（messages 重建为 tuple）。"""
    return state.with_(messages=(*state.messages, message))


def _plain_value(obj: DynamicValue) -> DynamicValue:
    """把单个值规范化为可序列化形态（确定性快照的一部分）。

    分支：
        - pydantic 模型：走 model_dump(mode='python')。
        - dict / list / tuple：递归规范化每个元素。
        - 其余标量：原样返回。
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if isinstance(obj, dict):
        return {k: _plain_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain_value(v) for v in obj]
    return obj


def _budget_delta(effect: Effect) -> BudgetDelta:
    """Effect 声明的单步预算；未声明时只扣 1 turn。

    分支：
        - 无 budget 声明：空 dict 起步。
        - turns 缺失时补默认 1（每个 Effect 至少消耗一个 turn）。
    """
    declared: dict[str, DynamicValue] = dict(effect.payload.get("budget") or {})
    declared["turns"] = declared.get("turns", 1)
    return cast(BudgetDelta, declared)


def _message_for(effect: Effect, result: Result) -> Message | None:
    """按 Effect 类型生成对话消息；返回 None 表示该类 Effect 不进 messages。

    分支：
        - 失败 Result：记 system 消息（错误码 + 信息），便于对话上下文感知故障。
        - LLM：记 assistant 消息（正文 + tool_calls），tool_calls 先规范化。
        - TOOL：记 tool 消息（结果信封规范化后 canonical 序列化，name=call_id）。
        - SKILL/CHILD/WAIT_HUMAN/END 等：不进 messages，由 fold 的托管字段承载。
    """
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
        # call_id 优先取调用对象内字段，回退到 payload 顶层声明。
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

    参数：
        state: 当前状态（不变）。
        effects: 本批已执行的 Effect，与 results 按位置一一对应。
        results: 本批 Effect 的执行结果。
        reason: 本步结束原因（写入 last_reason；空则沿用旧值）。

    返回：
        折叠后的新 AgentState。
    """
    messages: list[Message] = list(state.messages)  # 消息累积（从旧消息副本起步）
    budgets = state.budgets  # 预算累积（不可变，逐 Effect 替换）
    # 托管字段用显式局部变量（编译期可枚举），不用 **dict 拼装（编码规则 3）
    output: DynamicValue = state.output
    last_skill: DynamicValue = state.last_skill
    last_child: DynamicValue = state.last_child
    last_wait_human: DynamicValue = state.last_wait_human
    cost = 0.0  # 本轮 usage 累计成本，扣完预算后清零，避免跨 Effect 重复计入
    tokens = 0  # 本轮 usage 累计 token（prompt + completion）
    # effects 与 results 由 Runner 保证等长、同序，zip 逐一配对处理。
    for effect, result in zip(effects, results):
        # ① 生成对话消息（失败 / LLM / TOOL 才有）
        message = _message_for(effect, result)
        if message is not None:
            messages.append(message)
        # ② 特殊 kind 写入托管字段（互斥：同一 Effect 只可能命中其一）
        if effect.kind is EffectKind.SKILL:
            last_skill = _plain_value(result.data)
        elif effect.kind is EffectKind.CHILD:
            last_child = _plain_value(result.data)
        elif effect.kind is EffectKind.WAIT_HUMAN:
            last_wait_human = _plain_value(result.data)
        # END 且带 output 时写入最终输出（不与上面的 elif 冲突：END 是独立 kind）。
        if effect.kind is EffectKind.END and "output" in effect.payload:
            output = _plain_value(effect.payload["output"])
        # ③ 累计本 Effect 的 usage（成本/token），供下方统一计入预算。
        if result.usage is not None:
            cost += float(field(result.usage, "cost_usd", 0.0))
            tokens += int(field(result.usage, "prompt_tokens", 0)) + int(
                field(result.usage, "completion_tokens", 0)
            )
        # ④ 统一扣预算：effect 声明的 delta + 本步实际 usage。
        delta = _budget_delta(effect)
        budgets = budgets.consume(
            turns=int(delta.get("turns", 0)),
            hops=int(delta.get("hops", 0)),
            rounds=int(delta.get("rounds", 0)),
            cost_usd=float(delta.get("cost_usd", 0.0)) + cost,
            tokens=int(delta.get("tokens", 0)),
            parallel_children=int(delta.get("parallel_children", 0)),
        )
        cost = 0.0  # 已并入预算，清零以防下一个 Effect 重复累加

    # 一次性构造新 State：消息与 kind 序列转 tuple，满足 frozen 不可变约束。
    return state.with_(
        messages=tuple(messages), budgets=budgets,
        output=output, last_skill=last_skill, last_child=last_child,
        last_wait_human=last_wait_human,
        last_step_kinds=tuple(e.kind.value for e in effects),
        last_tokens=tokens,
        last_reason=reason or state.last_reason,
    )


def replay_events(events: Iterable[DynamicValue], initial_state: AgentState) -> AgentState:
    """事件溯源重放：逐步 fold 得到终态。

    参数：
        events: 事件序列，每项需有 effects/results/reason 属性或键。
        initial_state: 起始状态。

    返回：
        依次 fold 全部事件后的终态。
    """
    state = initial_state
    for event in events:  # 按事件顺序重放，保证与执行期轨迹一致
        state = fold(state, event.effects, event.results, getattr(event, "reason", ""))
    return state
