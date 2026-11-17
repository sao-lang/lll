"""R1 解释器：驱动 Program generator，只做确定性推进，不做任何 IO。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, cast

from .errors import ProgramCrashed, ProgramInvalid
from .types import DONE, DoneSentinel, DynamicValue, Effect, EffectPayload, Program

__all__ = ["EffectBatch", "Interpreter", "InterpreterImpl", "SendResult"]


@dataclass(frozen=True)
class EffectBatch:
    """一批待执行 Effect；batched=True 表示回填 Program 时传 list 而非单值。"""
    effects: tuple[Effect, ...]
    batched: bool = False


type SendResult = EffectBatch | DoneSentinel
"""解释器推进结果：下一批 Effect 或结束哨兵。"""


class Interpreter(Protocol):
    """解释器协议：send 一次推进一步。"""
    def send(self, value: DynamicValue) -> SendResult:
        """推进一步：value 是上一次 Effect 的结果，返回下一批 Effect 或 DONE。"""
        ...


def _expand(effect: Effect) -> tuple[Effect, ...]:
    """batch=True 的 Effect 按 payload['calls'] 展开为多个独立 Effect。"""
    if not effect.batch:
        return (effect,)
    calls = list(effect.payload.get("calls") or ())
    if not calls:
        return (replace(effect, batch=False),)
    payload: dict[str, DynamicValue] = {k: v for k, v in effect.payload.items() if k != "calls"}
    return tuple(
        Effect(effect.kind, cast(EffectPayload, {**payload, "call": call}),
               batch=False, trace_id=effect.trace_id)
        for call in calls
    )


class InterpreterImpl:
    """program.send(上次结果) → 下一批 Effect | DONE；预算检查在 Runner 层。"""
    def __init__(self, program: Program):
        """用 generator 构造解释器；非 generator 直接 ProgramInvalid。"""
        if not (hasattr(program, "send") and hasattr(program, "__next__")):
            raise ProgramInvalid("program must be a generator (State -> yield Effect)")
        self._gen = program
        self._started = False

    @property
    def started(self) -> bool:
        """generator 是否已被首次推进。"""
        return self._started

    def send(self, value: DynamicValue) -> SendResult:
        """推进一步：首次 next() 启动，其后 send(value) 回填上次结果。"""
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
            single = _expand(raw)
            return EffectBatch(single, batched=raw.batch or len(single) > 1)
        if isinstance(raw, (list, tuple)) and raw:
            items: tuple[DynamicValue, ...] = tuple(raw)
            if all(isinstance(e, Effect) for e in items):
                many: tuple[Effect, ...] = tuple(
                    expanded
                    for item in items
                    for expanded in _expand(cast(Effect, item))
                )
                return EffectBatch(many, batched=True)
        raise ProgramInvalid(
            f"program yielded {type(raw).__name__}; expected Effect | list[Effect]"
        )
