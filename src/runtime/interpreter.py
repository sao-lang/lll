"""解释器：驱动 Program generator，只做确定性推进，不做任何 IO。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, cast

from .errors import ProgramCrashed, ProgramInvalid
from .types import DONE, DoneSentinel, DynamicValue, Effect, EffectPayload, Program

__all__ = ["EffectBatch", "Interpreter", "InterpreterImpl", "SendResult"]


@dataclass(frozen=True)
class EffectBatch:
    """一批待执行 Effect；batched=True 表示回填 Program 时传 list 而非单个值。"""

    effects: tuple[Effect, ...]  # 本批 Effect 序列（batch 已展开）
    batched: bool = False  # 结果是否以列表形式回填


# send() 的返回：要么下一批 Effect，要么结束哨兵 DONE（不会是裸 Result）。
type SendResult = EffectBatch | DoneSentinel
"""解释器推进结果：下一批 Effect 或结束哨兵（用显式别名避免 Any 混入返回类型）。"""


class Interpreter(Protocol):
    """解释器协议：send 一次推进一步，返回 EffectBatch 或结束哨兵。"""

    def send(self, value: DynamicValue) -> SendResult: ...


def _expand(effect: Effect) -> tuple[Effect, ...]:
    """batch=True 的 Effect 按 payload["calls"] 展开为多个独立 Effect（结果按序回填）。"""
    if not effect.batch:
        return (effect,)
    calls = list(effect.payload.get("calls") or ())
    if not calls:
        return (replace(effect, batch=False),)
    payload: dict[str, DynamicValue] = {k: v for k, v in effect.payload.items() if k != "calls"}
    return tuple(
        Effect(
            effect.kind,
            cast(EffectPayload, {**payload, "call": call}),
            batch=False,
            trace_id=effect.trace_id,
        )
        for call in calls
    )


class InterpreterImpl:
    """`program.send(上次结果)` → 下一批 Effect | DONE；预算检查在 Runner 层。"""

    def __init__(self, program: Program):
        """用 generator 构造解释器；非 generator 对象直接拒绝（契约校验）。"""
        if not (hasattr(program, "send") and hasattr(program, "__next__")):
            raise ProgramInvalid("program must be a generator (State -> yield Effect)")
        self._gen = program
        self._started = False

    @property
    def started(self) -> bool:
        """generator 是否已被首次推进（首次用 next，之后用 send）。"""
        return self._started

    def send(self, value: DynamicValue) -> SendResult:
        """推进一步：首次 next() 启动 generator，其后 send(value) 把上次结果回填。

        StopIteration 表示 program 自然结束 → 返回 DONE；任何非 Effect 产出
        都是契约违规 → ProgramInvalid；其余异常折叠为 ProgramCrashed，绝不外泄。
        """
        try:
            raw = next(self._gen) if not self._started else self._gen.send(value)
        except StopIteration:
            return DONE
        except ProgramInvalid:
            raise
        except Exception as exc:
            raise ProgramCrashed(f"{type(exc).__name__}: {exc}") from exc
        self._started = True

        if isinstance(raw, Effect):
            effects = _expand(raw)
            return EffectBatch(effects, batched=raw.batch or len(effects) > 1)
        if isinstance(raw, (list, tuple)) and raw and all(isinstance(e, Effect) for e in raw):
            expanded = tuple(e for item in raw for e in _expand(item))
            return EffectBatch(expanded, batched=True)
        raise ProgramInvalid(
            f"program yielded {type(raw).__name__}; expected Effect | list[Effect]"
        )
