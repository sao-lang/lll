"""RED-Bug2：工具抛异常后必须发起第二轮 LLM 且 tool_call_id 严格配对。

复审复现 tmp/review_tool_fail.py / tmp/review_toolids2.py D：失败工具的 Result
错误信封原样进 tool 消息 content → _normalize_history 的 json.dumps(Result) 抛
TypeError → 在 _call_llm 内冒泡、被 Runner 折叠成失败 Result → 第二轮 LLM 根本
没发，用户拿到空串且无报错。本测试断言：答复非空、第二轮真发起、id 严格配对。
"""
from __future__ import annotations

from typing import Any

from api import Agent
from archs import ReAct
from compose import DangerLevel, LLMRequest, LLMResponse, PortCtx, ToolCall, ToolSpec, Usage
from harness.model import ModelCapability


class _ScriptedLLM:
    """合法注入的脚本 LLM：首轮必然两工具调用（一失败一成功），次轮给结论。"""

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls.append([dict(m) for m in req.messages])
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(call_id="e1", name="bad", args={}),
                    ToolCall(call_id="e2", name="ok", args={}),
                ],
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                model=self.name,
            )
        return LLMResponse(
            content="FINAL", tool_calls=[],
            usage=Usage(prompt_tokens=2, completion_tokens=1), model=self.name,
        )

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> object:
        raise NotImplementedError


def test_red_tool_failure_still_runs_second_llm_round() -> None:
    llm = _ScriptedLLM()

    def bad() -> str:
        raise RuntimeError("boom")

    agent = Agent(
        ReAct([ToolSpec(name="bad", danger=DangerLevel.SAFE),
               ToolSpec(name="ok", danger=DangerLevel.SAFE)], llm="fake"),
        session_id="tool-fail-red",
    )
    agent.use(ModelCapability(llm=llm))
    agent.register_tool("bad", bad)
    agent.register_tool("ok", lambda: "OK")

    answer = agent.say("go")

    assert answer == "FINAL", f"工具失败后仍应拿到结论，实际 {answer!r}"
    assert len(llm.calls) == 2, (
        f"工具失败后必须发起第二轮 LLM，实际 {len(llm.calls)} 次")
    second = llm.calls[1]
    assistant_ids = [t["id"] for m in second if m.get("role") == "assistant"
                     for t in m.get("tool_calls", [])]
    tool_ids = [m["tool_call_id"] for m in second if m.get("role") == "tool"]
    assert assistant_ids == ["e1", "e2"], f"助手 tool_call id 错：{assistant_ids}"
    assert tool_ids == ["e1", "e2"], f"tool 消息 id 错（配对断裂）：{tool_ids}"
    assert agent.last_run is not None and agent.last_run.status == "done"


def test_red_normalize_history_tolerates_failed_result() -> None:
    """失败 Result 进历史时必须在归一化边界降为字符串，不得 TypeError。"""
    from api.agent import _normalize_history
    from runtime import Result

    msgs: list[dict[str, Any]] = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"call_id": "e1", "name": "bad", "args": {}}]},
        {"role": "tool", "name": "bad", "call_id": "e1",
         "content": Result.failure("TOOL_ERR", "boom")},
    ]
    normalized = _normalize_history(msgs)  # type: ignore[arg-type]
    assert normalized[1]["tool_call_id"] == "e1"
    content = normalized[1]["content"]
    assert isinstance(content, str)
    assert "TOOL_ERR" in content and "boom" in content, (
        f"失败信封必须转成可读字符串，实际 {content!r}")
