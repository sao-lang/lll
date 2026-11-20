"""R12 算子核心测试辅助：手动驱动 Program generator，不依赖真实 Runner。

算子是装配期 Program→Program 的纯变换；测试直接推进 generator、注入模拟结果，
验证 Effect 编排序列与 output 传递，避免把 R1 Runner 的实现细节牵进算子单测。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from runtime import AgentState, Effect, Invoke, Program, Result, Terminate

# 模拟回注值统一用 AgentState（R1 fold 回注的就是新 State）。
Driver = Callable[[Effect, int], AgentState]


def drive(
    program: Program,
    *,
    initial: AgentState | None = None,
    responder: Driver | None = None,
) -> tuple[list[Effect], AgentState]:
    """手动驱动一个 Program，收集它 yield 的全部 Effect，返回终态 State。

    分支：
        - 遇到 Terminate：记录后停止（Terminate 是终态 Effect，不再回注）。
        - 遇到其他 Effect：调用 responder 产出模拟回注 State，send 回 generator。

    Args:
        program: 被测 Program（generator 函数）。
        initial: 初始 State，缺省为空 AgentState。
        responder: (effect, index) -> 回注 State；缺省回注 initial 原样。

    Returns:
        (effect 序列, 终态 State)——终态 State 的 output 即 program 产出。
    """
    state = initial or AgentState()
    # last_sent：最后成功 send 进 generator 的 State。算子内部的 x-* 修改经
    # _carry_x 合并到它上面，用它作终态基线才能观察到算子的 scratch 传递。
    last_sent = state
    effects: list[Effect] = []
    gen = program()
    try:
        effect = next(gen)
        index = 0
        while True:
            # port="state" 是组合层内部的引导 Effect（首次回注拿初始 State），
            # 不是对外可观测的业务 Effect，不收集进断言序列。
            is_probe = isinstance(effect, Invoke) and effect.port == "state"
            if not is_probe:
                # 并行 Effect 批（list）逐项展开收集，与 R1 interpreter 的
                # EffectBatch.batched 语义一致（interpreter.py:153-162）。
                if isinstance(effect, (list, tuple)):
                    effects.extend(effect)
                else:
                    effects.append(effect)
            if isinstance(effect, Terminate):
                # 模拟 R1 fold：Terminate 的 output 写入 state.output（唯一托管写入点）
                state = last_sent.set("output", effect.output)
                break
            state = (responder or (lambda e, i: state))(effect, index)
            effect = gen.send(state)
            # batch 回注是 list（R1 runner.py:524-526），不是 State，不能作 Terminate 基线；
            # 保留上一个真实 State 作为终止输出的承载（与 R1 last_value 语义一致）。
            if isinstance(state, AgentState):
                last_sent = state
            index += 1
    except StopIteration:
        state = last_sent
    return effects, state


def responder_output(value: object, *, key: str = "slot") -> Driver:
    """构造把上次结果写入 scratch 指定键的回注器（供多步算子传递数据）。"""
    def _respond(effect: Effect, index: int) -> AgentState:
        base = AgentState()
        return base.set(key, value)
    return _respond


def leaf(name: str, *, output: object | None = None) -> Program:
    """构造一个叶子 Program：yield 一个带 name 的 Invoke，随后 Terminate。"""
    def program() -> Iterator[Effect]:
        yield Invoke(port="llm", payload={"name": name})
        yield Terminate(output=output if output is not None else name)
    return program


def __getattr__(name: str) -> Any:  # pragma: no cover - 兼容旧测试的 Result 导入
    if name == "Result":
        return Result
    raise AttributeError(name)
