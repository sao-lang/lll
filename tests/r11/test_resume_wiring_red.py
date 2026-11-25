"""RED：门面 HITL 闭环——say() 挂起后 agent.resume(answer) 续跑。

裂缝：R1 Runner.resume 与 wait_human 相位存在，但门面无 resume 入口——
say() 遇挂起返回空串、会话丢失，HITL 在语义门面上不可达。
"""
from __future__ import annotations

import pytest

from api import Agent
from compose import LifecyclePhase
from runtime import Invoke, Suspend, Terminate
from harness.model import ModelCapability


class _LLM:
    name = "fake"
    last_usage = None

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="已按答复执行", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _gate_arch() -> object:
    """可挂起架构：直接 yield Suspend（gate 算子已因依赖不存在的 state port 删除）。"""
    from types import SimpleNamespace

    def program():  # type: ignore[no-untyped-def]
        answer = yield Suspend(question="approval", options=("yes", "no"))
        yield Invoke(port="llm", payload={"name": "g", "answer": answer})
        yield Terminate(output="g-done")

    return SimpleNamespace(compile=program)


def test_red_say_suspends_and_resume_completes() -> None:
    """gate 架构：say 挂起（不返回假答案），resume 后真续跑出结果。"""
    agent = Agent(_gate_arch(), session_id="hitl-1")  # type: ignore[arg-type]
    agent.use(ModelCapability(llm=_LLM()))

    first = agent.say("开始审批流程")
    assert "wait_human" in first or "人工" in first, (
        f"挂起应返回明确等待语义，实得 {first!r}")
    assert agent.last_run is not None and agent.last_run.status == "suspended"

    phases: list[LifecyclePhase] = []
    agent.on(LifecyclePhase.RESUMED, lambda n: phases.append(n.phase))
    second = agent.resume("yes")

    assert second == "g-done", f"resume 应续跑出结果，实得 {second!r}"
    assert agent.last_run.status == "done"
    assert LifecyclePhase.RESUMED in phases, f"resumed 相位未广播：{phases}"
    # 连续性：原 say 输入 + 最终回答成对进历史
    assert agent.history[-2]["content"] == "开始审批流程"
    assert agent.history[-1]["content"] == "g-done"


def test_red_resume_without_pending_raises() -> None:
    """无挂起会话时 resume 必须 fail-closed 报错，不静默。"""
    agent = Agent(_gate_arch(), session_id="hitl-2")  # type: ignore[arg-type]
    agent.use(ModelCapability(llm=_LLM()))
    with pytest.raises(RuntimeError):
        agent.resume("yes")
