"""解释器：驱动 Program generator，只做确定性推进，不做任何 IO。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from .errors import ProgramCrashed, ProgramInvalid
from .types import DONE, Effect, Program

__all__ = ["EffectBatch", "Interpreter", "InterpreterImpl"]


@dataclass(frozen=True)
class EffectBatch:
    """一批待执行 Effect；batched=True 表示回填 Program 时传 list 而非单个值。"""

    effects: tuple[Effect, ...]
    batched: bool = False


class Interpreter(Protocol):
    def send(self, value: Any) -> EffectBatch | Any: ...


def _expand(effect: Effect) -> tuple[Effect, ...]:
    """batch=True 的 Effect 按 payload["calls"] 展开为多个独立 Effect（结果按序回填）。"""
    if not effect.batch:
        return (effect,)
    calls = list(effect.payload.get("calls") or ())
    if not calls:
        return (replace(effect, batch=False),)
    payload = {k: v for k, v in effect.payload.items() if k != "calls"}
    return tuple(
        Effect(effect.kind, {**payload, "call": call}, batch=False, trace_id=effect.trace_id)
        for call in calls
    )


class InterpreterImpl:
    """`program.send(上次结果)` → 下一批 Effect | DONE；预算检查在 Runner 层。"""

    def __init__(self, program: Program):
        if not (hasattr(program, "send") and hasattr(program, "__next__")):
            raise ProgramInvalid("program must be a generator (State -> yield Effect)")
        self._gen = program
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def send(self, value: Any) -> EffectBatch | Any:
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
