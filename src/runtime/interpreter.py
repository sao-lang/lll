"""R1 解释器：驱动 Program generator，只做确定性推进，不做任何 IO。

职责边界：把 generator 的一次推进（next / send）翻译为"下一批 Effect 或 DONE"。
batch 展开（一个 Effect 携带多个 calls → 多个独立 Effect）也在此完成，
保证 Runner 拿到的永远是扁平、可并行的 Effect 批次。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, cast

from .errors import ProgramCrashed, ProgramInvalid
from .types import DONE, DoneSentinel, DynamicValue, Effect, EffectPayload, Program

__all__ = ["EffectBatch", "Interpreter", "InterpreterImpl", "SendResult"]


@dataclass(frozen=True)
class EffectBatch:
    """一批待执行 Effect；batched=True 表示回填 Program 时传 list 而非单值。

    字段：
        effects: 本批要交给 Runner 执行的 Effect（tuple 保证不可变）。
        batched: Program 当初是"批量 yield"还是单 yield；决定 send 回填的是
                 list[结果] 还是单个结果。batch 展开后 >1 个也置 True。
    """
    effects: tuple[Effect, ...]
    batched: bool = False


type SendResult = EffectBatch | DoneSentinel
"""解释器推进结果：下一批 Effect（EffectBatch）或结束哨兵（DONE）。"""


class Interpreter(Protocol):
    """解释器协议：send 一次推进一步。"""

    def send(self, value: DynamicValue) -> SendResult:
        """推进一步。

        参数：
            value: 上一次 yield 的 Effect 的执行结果；首次推进时忽略（内部走 next）。

        返回：
            EffectBatch（下一批待执行 Effect）或 DONE（generator 已 return）。
        """
        ...


def _expand(effect: Effect) -> tuple[Effect, ...]:
    """batch=True 的 Effect 按 payload['calls'] 展开为多个独立 Effect。

    分支：
        - 非 batch：原样返回单个 Effect。
        - batch 但无 calls：退化为单个非 batch Effect，避免空批次。
        - batch 且有 calls：剔除 calls 键后，逐个 call 生成独立 Effect，
          每个 payload 用 'call' 单值承载，trace_id 原样透传。
    """
    if not effect.batch:  # 非批量：无需展开
        return (effect,)
    calls = list(effect.payload.get("calls") or ())  # 待展开的调用列表
    if not calls:  # 批量声明为空：降级为单 Effect，避免产出空批次
        return (replace(effect, batch=False),)
    # 展开时移除 calls 聚合键，避免每个子 Effect 又携带整份 calls。
    payload: dict[str, DynamicValue] = {k: v for k, v in effect.payload.items() if k != "calls"}
    return tuple(
        Effect(
            effect.kind,
            cast(EffectPayload, {**payload, "call": call}),  # 每个 call 变单值 payload
            batch=False,  # 子 Effect 已展开，不再是 batch
            trace_id=effect.trace_id,  # 保留链路溯源
        )
        for call in calls
    )


class InterpreterImpl:
    """program.send(上次结果) → 下一批 Effect | DONE；预算检查在 Runner 层。"""

    def __init__(self, program: Program):
        """用 generator 构造解释器；非 generator 直接 ProgramInvalid。

        参数：
            program: 用户 Agent 逻辑 generator（读 State → yield Effect → 收结果）。

        异常：
            ProgramInvalid: program 缺少 send/__next__，即不是 generator。
        """
        # generator 必须同时具备 send 与 __next__，否则后续推进无从谈起。
        if not (hasattr(program, "send") and hasattr(program, "__next__")):
            raise ProgramInvalid("program must be a generator (State -> yield Effect)")
        self._gen = program  # 底层 generator
        self._started = False  # 是否已完成首次 next()（首推用 next，其后用 send）

    @property
    def started(self) -> bool:
        """generator 是否已被首次推进（True 表示后续应走 send 而非 next）。"""
        return self._started

    def send(self, value: DynamicValue) -> SendResult:
        """推进一步：首次 next() 启动，其后 send(value) 回填上次结果。

        参数：
            value: 上次 Effect 的执行结果；首次推进忽略（generator 尚未启动不能 send）。

        返回：
            EffectBatch 或 DONE。

        异常：
            ProgramInvalid: yield 了非 Effect / 非 Effect 列表。
            ProgramCrashed: Program 内部抛出未预期异常（已包装）。
        """
        try:
            # 首次必须 next() 启动 generator；之后才能 send(value) 回填结果。
            raw = next(self._gen) if not self._started else self._gen.send(value)
        except StopIteration:
            return DONE  # generator return ⇒ 程序结束
        except ProgramInvalid:
            raise  # 契约违规原样抛出，交由 Runner 落入报告
        except Exception as exc:
            # 任何未预期异常折叠为 ProgramCrashed，绝不裸抛穿运行时。
            raise ProgramCrashed(f"{type(exc).__name__}: {exc}") from exc
        self._started = True  # 推进成功，标记已启动

        if isinstance(raw, Effect):
            single = _expand(raw)  # 单 Effect（可能是 batch，需展开）
            return EffectBatch(single, batched=raw.batch or len(single) > 1)
        # Program 也允许一次 yield 一个 Effect 列表（并行声明）。
        if isinstance(raw, (list, tuple)) and raw:
            items: tuple[DynamicValue, ...] = tuple(raw)
            if all(isinstance(e, Effect) for e in items):
                # 逐项展开后再合并：列表里每个 Effect 自己可能也是 batch。
                many: tuple[Effect, ...] = tuple(
                    expanded
                    for item in items
                    for expanded in _expand(cast(Effect, item))
                )
                return EffectBatch(many, batched=True)
        # 走到这里说明 yield 了非法对象（含空列表），按契约违规处理。
        raise ProgramInvalid(
            f"program yielded {type(raw).__name__}; expected Effect | list[Effect]"
        )
