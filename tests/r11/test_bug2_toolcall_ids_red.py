"""RED-Bug2: 同轮并行调用同名工具，tool_call_id 必须一一对应。

审计 P10：ReAct 给 tool 消息不写 call_id；_normalize_history 又按 name
最后写入胜出回填 → 两条 tool 消息都挂 c2，c1 无结果。OpenAI-compatible
后端按 tool_call_id 配对，缺/重即 400。修：tool 消息写 call_id，归一化
按 call_id 原样回填，不按 name 猜。
"""
from __future__ import annotations

from api import Agent
from archs import ReAct
from compose import DangerLevel, LLMResponse, ToolCall, ToolSpec, Usage
from harness.model import ModelCapability


class _StrictPairingLLM:
    """模拟 OpenAI-compatible 后端：assistant 的 call id 与 tool 消息必须一一对应。"""

    name = "strict-pairing"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.observed: list[tuple[list[str], list[str]]] = []
        self.pairing_errors: list[tuple[list[str], list[str]]] = []
        self.script = [
            {"content": "", "tool_calls": [
                ("t", {"x": "1"}, "c1"), ("t", {"x": "2"}, "c2"),
            ]},
            {"content": "FINAL", "tool_calls": []},
        ]

    async def complete(self, req: object, ctx: object) -> LLMResponse:
        messages = [dict(m) for m in getattr(req, "messages", [])]
        assistant_ids: list[str] = []
        tool_ids: list[str] = []
        for m in messages:
            if m.get("role") == "assistant":
                assistant_ids = [str(tc.get("id") or tc.get("call_id") or "")
                                 for tc in (m.get("tool_calls") or [])]
            elif m.get("role") == "tool":
                tool_ids.append(str(m.get("tool_call_id") or ""))
        if assistant_ids or tool_ids:
            pair = (list(assistant_ids), list(tool_ids))
            self.observed.append(pair)
            if sorted(assistant_ids) != sorted(tool_ids):
                self.pairing_errors.append(pair)
                raise RuntimeError(
                    f"400 tool_call_id mismatch: assistant={assistant_ids} tool={tool_ids}")

        snap = {"messages": messages}
        self.calls.append(snap)
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        calls = [ToolCall(name=n, args=a, call_id=c) for n, a, c in step["tool_calls"]]
        return LLMResponse(content=step["content"], tool_calls=calls,
                           usage=Usage(prompt_tokens=2, completion_tokens=2,
                                       cost_usd=0.002), model="strict")

    async def stream(self, req: object, ctx: object) -> object:
        raise NotImplementedError


def test_red_parallel_same_name_tools_pair_by_call_id() -> None:
    """并行两次同名工具：下一轮 LLM 收到 c1/c2 各一条 tool 消息。"""
    llm = _StrictPairingLLM()
    executed: list[str] = []

    def tool_fn(x: str = "") -> str:
        executed.append(x)
        return f"R:{x}"

    agent = Agent(ReAct([ToolSpec(name="t", danger=DangerLevel.SAFE)], llm="fake"),
                  session_id="bug2-pairing")
    agent.use(ModelCapability(llm=llm)).register_tool("t", tool_fn)

    answer = agent.say("two calls")

    assert not llm.pairing_errors, (
        f"tool_call_id 必须一一对应：{llm.pairing_errors}；实收 {llm.observed}")
    assert answer == "FINAL", f"严格后端校验通过后才应出真实答复，实得 {answer!r}"
    assert llm.observed == [(["c1", "c2"], ["c1", "c2"])], (
        f"精确断言 id 配对，实得 {llm.observed}")
    assert executed == ["1", "2"], f"两次工具都须真执行，实得 {executed}"
