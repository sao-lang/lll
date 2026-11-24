"""真 bug 回归：重复工具名发给 OpenAI-compatible 后端 → 400 → 回答为空。

披露包（A4）与架构 tools 可能含同名 MCP 工具；框架 _call_llm 必须按 name 去重，
否则 DeepSeek 等返回 400，用户看到空回答（status=done 但 output=None）。
本测试用假 LLM 捕获实际 tools_catalog，断言无重复——不依赖外网。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from api import Agent  # noqa: E402
from archs import ReAct  # noqa: E402
from compose import LLMRequest, LLMResponse, PortCtx, ToolSpec, Usage  # noqa: E402
from harness.model import ModelCapability  # noqa: E402


class _SpyLLM:
    name = "spy"
    last_usage = None

    def __init__(self) -> None:
        self.tool_names: list[list[str]] = []

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.tool_names.append([t.name for t in (req.tools_catalog or [])])
        return LLMResponse(content="ok", tool_calls=[],
                           usage=Usage(prompt_tokens=1, completion_tokens=1,
                                       cost_usd=0.0), model="spy")

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> object:  # pragma: no cover
        raise NotImplementedError


def test_regression_duplicate_tool_names_deduped() -> None:
    llm = _SpyLLM()
    agent = Agent(ReAct([], llm="spy"), session_id="dedup")
    agent.use(ModelCapability(llm=llm))
    # 模拟披露包含重复工具名（A4 与架构 tools 叠加的真实场景）
    spec = ToolSpec(name="dup_tool", description="d", parameters_schema={})
    agent._disclosed_tools = [spec, spec]  # type: ignore[assignment]
    agent.register_tool("dup_tool", lambda: "x")

    asyncio.run(agent.asay("你好"))

    assert llm.tool_names, "LLM 未被调用"
    names = llm.tool_names[0]
    assert names.count("dup_tool") == 1, (
        f"重复工具名未去重（会导致 OpenAI-compatible 后端 400）：{names}")
