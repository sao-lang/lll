"""ToolLayer 统一外壳（规格 §4 / P4-P8）：每个能力层 build() -> Program。

Program 是 R1 唯一认识的领域无知载体（Generator[Effect, DynamicValue, None]）。
各 harness/*/capability.py 只持业务实现，外壳把"使用该能力"表达为可组合的 Program：
一个 port 对应的 Invoke + Terminate。A 类算子（seq/branch/...）在装配期组合这些
Program，R1 只认 Program、零领域感知（D-7）。

内部业务逻辑（gateway.complete / RAGPipeline.query / ContextMemoryManager.assemble 等）
保持不变，由组合层 dispatch 按 port 分派到对应 Port 时消费 payload。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from runtime import DynamicValue, Effect, Invoke, Program, Terminate

__all__ = ["layer_program"]


def layer_program(
    port: str,
    *,
    payload: Mapping[str, DynamicValue] | None = None,
    output: DynamicValue = None,
) -> Callable[[], Program]:
    """构造一个 ToolLayer 外壳 Program 工厂（build() 的返回值）。

    Args:
        port: 该能力层对应的 Effect.port（dispatch 按此分派到 Port）。
        payload: 随 Invoke 携带的只读输入（缺省空；业务参数由 dispatch/ctx 补充）。
        output: Terminate 承载的输出（缺省 None；组合算子按此串联）。

    Returns:
        Program 工厂（每次调用产出新 generator，resume 可重建，D-8）。

    用法：
        class MyCapability:
            def build(self) -> Program:
                return layer_program("rag", payload={"layer": "rag"})()
    """
    data: dict[str, DynamicValue] = dict(payload or {})

    def program() -> Program:
        yield Invoke(port=port).with_payload(**data)
        yield Terminate(output=output)
        return None

    return program
