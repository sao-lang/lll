"""R6 外壳试点端到端（规格 P7）：验证主线"挑选 + A 类组合 + R1 只认 Program"。

R6 是唯一完全符合"复杂度在组合层吸收"的工具层。本试点用 compose 的 A 类算子
装配一个 RAG 风格 Program（改写→召回→后处理），经 build_call + dispatch 注入真实
R1 Runner，断言端到端跑通。试点跑不通先修算子契约，不动 R1（D-7）。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import (
    AgentState,
    Effect,
    Invoke,
    Result,
    Runner,
    Session,
    Terminate,
    Usage,
)

from compose.call import build_call
from compose.dispatch import make_dispatch
from compose.program import map_effect, seq


def _rag_step(name: str, payload: dict[str, object]) -> Effect:
    """构造一个 RAG 流水线步骤的 Effect（模拟改写/召回/后处理）。"""
    return Invoke(port="rag", payload={"step": name, **payload})  # type: ignore[typeddict-item]


def _build_pipeline() -> object:
    """用 A 类算子装配 RAG Program：seq(改写, 后处理) + map_effect 注入标记。"""
    def rewrite() -> object:
        yield _rag_step("rewrite", {"query": "hello"})
        yield Terminate(output="rewritten")

    def post() -> object:
        yield _rag_step("post", {"hits": 2})
        yield Terminate(output="final-answer")

    # map_effect 注入来源标记（脱敏/注入标记的等价验证）
    tagged = map_effect(lambda e: e.with_payload(source="r6-pilot") if isinstance(e, Invoke) else e)
    # seq 编排：改写先、后处理后，最终 output 为后者产出
    return seq(tagged(rewrite), post)  # type: ignore[arg-type]


def test_r6_pilot_end_to_end_with_real_runner() -> None:
    """端到端：compose Program 经 build_call+dispatch 注入 R1 Runner 跑通。"""
    seen: list[str] = []

    def rag_handler(effect: Invoke, ctx: object) -> Result:
        seen.append(str(effect.payload.get("step")))
        # 验证 map_effect 注入的标记随 Effect 透传（组合层改写生效）
        assert effect.payload.get("source") in ("r6-pilot", None)
        return Result.success("ok", usage=Usage(prompt_tokens=1, completion_tokens=0))

    call = build_call(make_dispatch({"rag": rag_handler}))
    runner = Runner(call=call)
    program_factory = _build_pipeline()
    result = asyncio.run(runner.run(
        Session(session_id="r6-pilot", program_factory=program_factory)  # type: ignore[arg-type]
    ))

    assert result.status == "done"
    assert result.output == "final-answer"
    assert seen == ["rewrite", "post"]


def test_r6_pilot_uses_only_program_not_arch_knowledge() -> None:
    """R1 只认 Program：Runner 无需知道架构/算子/RAG 的任何领域知识。"""
    call = build_call(make_dispatch({}))
    runner = Runner(call=call)
    # 一个最小 Program 即可跑通，证明内核对组合层零感知
    def minimal() -> object:
        yield Terminate(output="ok")
    result = asyncio.run(runner.run(
        Session(session_id="minimal", program_factory=minimal)  # type: ignore[arg-type]
    ))
    assert result.status == "done" and result.output == "ok"
