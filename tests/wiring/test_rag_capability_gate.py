"""B1 行为门禁：RAGCapability 可注入且真进执行链。

用户注入自己的检索工具（Retriever）到 RAGCapability → Agent.use() →
架构 Invoke(port="rag") 真调到注入的检索实现（不是死对象）。
"""
from __future__ import annotations

import asyncio

from api import Agent
from archs import ReAct
from compose import PortCtx, ToolSpec
from harness.model import ModelCapability
from harness.rag import RAGPipeline


class _SpyRetriever:
    """注入的检索工具：记录调用并返回带溯源的命中。"""
    name = "spy_retriever"
    calls: list[str] = []

    async def retrieve(self, query: object, vec: object) -> list[object]:
        self.calls.append(str(getattr(query, "text", query)))
        return []


class _LLM:
    name = "fake"
    last_usage = None
    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")
    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_gate_b1_rag_capability_reaches_execution() -> None:
    from harness.rag.capability import RAGCapability

    retriever = _SpyRetriever()
    pipeline = RAGPipeline.with_retriever(retriever) if hasattr(
        RAGPipeline, "with_retriever") else _build_pipeline(retriever)
    cap = RAGCapability(pipeline=pipeline)

    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), session_id="gate-b1")
    agent.use(ModelCapability(llm=_LLM()))
    agent.use(cap)
    asyncio.run(agent._arun())

    handler = agent._port_handlers.get("rag")
    assert handler is not None, "RAGCapability 的 provider 未进 handler 表"
    # 真调 handler（模拟架构 Invoke(port=rag)）
    from runtime import Invoke
    result = asyncio.run(handler(Invoke(port="rag").with_payload(
        query="天气"), PortCtx(session_id="gate-b1")))
    assert result.ok
    assert retriever.calls, "注入的检索工具未被真调用"


def _build_pipeline(retriever: object) -> object:
    """用 RAGPipeline 真实构造参数（五段必填）构造，retriever 注入。"""
    from harness.rag import (HashEmbedder, InMemoryVectorStore,
                             RecursiveSplitter, DefaultPostProcessor)
    return RAGPipeline(
        splitter=RecursiveSplitter(),
        embedder=HashEmbedder(),
        store=InMemoryVectorStore(),
        retriever=retriever,  # type: ignore[arg-type]
        post=DefaultPostProcessor(),
    )
