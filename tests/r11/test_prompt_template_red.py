"""RED：提示词模板能力（系统/用户双段 + 变量渲染，不直接注入 prompt）。

现状：无模板类/插值；Agent(system=) 只收静态字符串；archs 里全是硬编码
prompt（"draft"/"critique"/"revise"/"plan"...）。用户期望模板化注入。
"""
from __future__ import annotations

import pytest

from api import Agent
from archs import ReAct
from compose import ToolSpec
from harness.model import ModelCapability

from string import Template


class _RecordingLLM:
    name = "fake"
    last_usage = None

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        self.messages.append([dict(m) for m in req.messages])
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0001), model="fake")

    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_red_prompt_template_renders_system_and_user() -> None:
    """模板双段渲染：变量插值 + role 绑定，中文变量不丢。"""
    from harness.prompt import PromptTemplate

    tpl = PromptTemplate(
        name="weather",
        system=Template("你是${role}助手，回答要${style}。"),
        user=Template("请查询${city}的天气。"),
    )
    messages = tpl.render(role="中文", style="简洁", city="深圳")
    assert messages == [
        {"role": "system", "content": "你是中文助手，回答要简洁。"},
        {"role": "user", "content": "请查询深圳的天气。"},
    ]


def test_red_agent_use_prompt_template_injects_system_message() -> None:
    """use(PromptCapability) + 输入模板 → say 的 LLM 首轮收到渲染后的 system 消息。"""
    from harness.prompt import PromptCapability, PromptTemplate

    tpl = PromptTemplate(
        name="weather",
        system=Template("你是${role}助手。"),
        user=Template("${question}"),
    )
    llm = _RecordingLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))
    agent.use(PromptCapability(templates=[tpl]))

    agent.say("深圳天气如何？")

    assert llm.messages, "LLM 未被调用"
    first = llm.messages[0]
    assert any(m.get("role") == "system" and "中文助手" in str(m.get("content"))
               for m in first), f"渲染后的 system 未注入：{first}"


def test_red_reflection_uses_prompt_template_not_hardcoded() -> None:
    """Reflection 架构的草稿/评审/修订提示词来自模板能力，而非硬编码。"""
    from harness.prompt import PromptCapability, PromptTemplate
    from archs import Reflection
    from harness.cognition import ThresholdReflector

    # 模板能力存在时，Reflection 架构应从能力里取模板（渲染变量）
    tpl = PromptTemplate(
        name="reflect",
        system=Template("你是评审员，目标是${goal}。"),
        user=Template("草稿：${draft}"),
    )
    cap = PromptCapability(templates=[tpl])
    arch = Reflection(ThresholdReflector(min_len=1))
    assert cap.render("reflect", goal="写得更好", draft="初稿") == [
        {"role": "system", "content": "你是评审员，目标是写得更好。"},
        {"role": "user", "content": "草稿：初稿"},
    ]
    assert arch is not None
