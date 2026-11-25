"""回归保护：全部 10 个内置架构的 say_stream 与同步 say 语义等价。

背景：此前"全架构 say_stream"只有 ReAct 一个测试（test_say_stream_arch_red.py），
其余 9 个架构的流式行为无测试证据。核查发现 say_stream 走统一 Program（架构零
改动），流式层用 ContextVar+Queue 投递 LLM 增量，尾部用 _extract_answer 兜底——
因此对所有架构，流式聚合结果必须与同步 say 完全一致，且都不崩。

本测试用各架构在既有测试中验证过的策略配置，parameterize 10 个架构：
    assert "".join([c async for c in agent.say_stream(...)]) == agent_say(...)
注意：部分架构（ToolUse/PlanExecute/Supervisor/Blackboard）按设计不产出文本，
say 与 say_stream 都返回空——这是架构语义，不是 bug；本测试守的是"流式与同步
等价"，不是"每个架构都必须吐字"。

首次运行即绿（守卫型回归测试，非 RED 修复证据）；未来若某架构的流式投递或
兜底逻辑漂移导致与 say 不等价，本测试会精确指出是哪个架构。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from api import Agent
from archs import (
    Blackboard, Graph, Handoff, PlanExecute, Reflection, ReAct,
    Router, RouterSkill, Supervisor, ToolUse,
)
from compose import DangerLevel, LLMResponse, ToolSpec, Usage
from harness.cognition.contracts import IntentResult
from harness.cognition.services import RuleExecutor, RulePlanner, ThresholdReflector
from harness.model import ModelCapability


class _AnswerLLM:
    """合规 LLM 替身：name/usage 齐全（缺 name 会被 ModelGateway 当默认后端跳过）。"""
    name = "answer"

    def __init__(self) -> None:
        self.last_usage: Usage | None = None
        self.calls = 0

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="OK", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.001), model="fake")

    def stream(self, req: object, ctx: object) -> Any:
        # LLMPort 契约：普通 def 返回 AsyncIterator（非 async def），否则会泄漏
        # coroutine。本替身只提供 complete，直接抛错触发门面 complete 回退路径。
        raise NotImplementedError


class _AsyncIntent:
    """异步意图策略：Router/RouterSkill 在活 loop 内 compile 的已验证形态。"""
    name = "async_intent"

    async def recognize(self, text: str, ctx: object) -> IntentResult:
        return IntentResult(label="a", confidence=0.9)


def _tools() -> list[ToolSpec]:
    return [ToolSpec(name="t", description="d", danger=DangerLevel.SAFE)]


def _react() -> ReAct:
    return ReAct(_tools(), llm="fake")


def _graph() -> Graph:
    g = Graph()
    g.node("a", lambda: _react().compile()).exit("a")  # type: ignore[arg-type]
    return g


# 每个架构的构造配方都取自既有测试中验证过的配置（非臆造 mock）
_ARCH_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("ToolUse", lambda: ToolUse(_tools(), parallel=False)),
    ("ReAct", _react),
    ("PlanExecute", lambda: PlanExecute(RulePlanner(";"), RuleExecutor())),
    ("Reflection", lambda: Reflection(
        ThresholdReflector(min_len=1, pass_marker="PASS"), max_rounds=1)),
    ("Router", lambda: Router(_AsyncIntent(), {"a": _react()}, default="a")),
    ("RouterSkill", lambda: RouterSkill(
        _AsyncIntent(), {"a": _react()}, default=_react())),
    ("Supervisor", lambda: Supervisor(RulePlanner(";"), _react())),
    ("Handoff", lambda: Handoff({"r": _react()})),
    ("Blackboard", lambda: Blackboard([_react()], rounds=1)),
    ("Graph", _graph),
]


def _make_agent(factory: Callable[[], Any], session_id: str) -> tuple[Agent, _AnswerLLM]:
    llm = _AnswerLLM()
    agent = Agent(factory(), session_id=session_id)
    agent.use(ModelCapability(llm=llm))
    # 工具架构需要真实 tool handler；一次注册，避免重复触发 fail-closed 去重
    agent.register_tool("t", lambda **kw: {"ok": True, "value": 1})
    return agent, llm


@pytest.mark.parametrize("arch_name,factory", _ARCH_FACTORIES, ids=[n for n, _ in _ARCH_FACTORIES])
def test_say_stream_matches_say_for_every_arch(arch_name: str,
                                                factory: Callable[[], Any]) -> None:
    """流式聚合必须与同步 say 完全一致，且两条路径都不崩。"""
    async def run() -> None:
        sync_agent, _ = _make_agent(factory, f"{arch_name}-say")
        sync_answer = sync_agent.say("hi")
        assert sync_agent.last_run is not None
        assert sync_agent.last_run.status == "done", (
            f"{arch_name}: say 应 done，实际 {sync_agent.last_run.status} "
            f"error={sync_agent.last_run.error}")

        stream_agent, _ = _make_agent(factory, f"{arch_name}-stream")
        chunks = [c async for c in stream_agent.say_stream("hi")]
        assert stream_agent.last_run is not None
        assert stream_agent.last_run.status == "done", (
            f"{arch_name}: say_stream 应 done，实际 {stream_agent.last_run.status} "
            f"error={stream_agent.last_run.error}")

        assert "".join(chunks) == sync_answer, (
            f"{arch_name}: 流式聚合 {''.join(chunks)!r} 应等于同步 say {sync_answer!r}")

    asyncio.run(run())


def test_all_ten_builtin_archs_covered() -> None:
    """防止新增架构后本测试漏掉（覆盖面自身也要被守护）。"""
    ids = {n for n, _ in _ARCH_FACTORIES}
    expected = {"ToolUse", "ReAct", "PlanExecute", "Reflection", "Router",
                "RouterSkill", "Supervisor", "Handoff", "Blackboard", "Graph"}
    assert ids == expected, f"内置架构覆盖不全：缺 {expected - ids}，多 {ids - expected}"
