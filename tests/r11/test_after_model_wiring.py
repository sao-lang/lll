"""边界1：after_model 相位接真实 LLM 装配（TDD RED → GREEN）。

- 常规测试：fake LLM 证明 `AgentRuntime.model_gateway(llm)` 用同一 bus 发 AFTER_MODEL
- 真实烟测：仅当 DEEPSEEK_API_KEY 环境变量存在时跑一次真实调用（默认跳过，省成本）
"""
from __future__ import annotations

import asyncio
import os

import pytest

from api import STREAM_TOPIC, assemble_runtime
from compose import LLMRequest, LLMResponse, PortCtx
from harness.model import ModelGateway
from harness.observability import InMemoryEventStream
from runtime import Usage


class _FakeLLM:
    """确定性 LLM 替身（fake 仅在 tests/）。"""
    name = "fake_llm"
    last_usage: Usage | None = None

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="pong", model="fake")

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> object:
        raise NotImplementedError
        yield  # pragma: no cover


def test_model_gateway_emits_after_model_on_same_bus() -> None:
    """model_gateway(llm) 用同一 bus：真实网关调用后 AFTER_MODEL 事件可订阅。"""
    stream = InMemoryEventStream()
    rt = assemble_runtime(stream=stream)
    llm = _FakeLLM()
    gw = rt.model_gateway(llm)
    assert isinstance(gw, ModelGateway)

    # 生产路径走 as_dispatch()（_center 内发 BEFORE/AFTER_MODEL），非直接 complete()
    from runtime import Invoke
    dispatch = gw.as_dispatch()
    result = asyncio.run(dispatch(
        Invoke(port="llm", payload={"messages": [{"role": "user", "content": "ping"}]}),
        PortCtx(session_id="s-llm")))
    assert result.ok, f"网关 dispatch 应成功: {result.error}"
    assert llm.calls == 1, "真实 LLM 后端应被调用"

    async def drain() -> list[object]:
        return [e async for e in stream.subscribe(STREAM_TOPIC)]
    events = asyncio.run(drain())
    assert any(getattr(e, "type", "") == "after_model" for e in events), (
        f"after_model 应经同一 bus 发布到事件流，实际 {[getattr(e,'type','') for e in events]}")


@pytest.mark.skipif(not os.getenv("DEEPSEEK_API_KEY"),
                    reason="真实 DeepSeek 烟测需 DEEPSEEK_API_KEY 环境变量（默认跳过，省成本）")
def test_real_deepseek_once() -> None:
    """真实 DeepSeek 单次烟测：验证 OpenAI-compatible 适配器 + after_model 真链路。

    密钥只从环境变量读取，绝不写入代码/测试/文档/日志。只调用一次。
    """
    from ports.adapters.llm import OpenAICompatLLM

    key = os.environ["DEEPSEEK_API_KEY"]
    stream = InMemoryEventStream()
    rt = assemble_runtime(stream=stream)
    llm = OpenAICompatLLM(
        base_url="https://api.deepseek.com/v1",
        api_key=key,
        model="deepseek-chat",
    )
    gw = rt.model_gateway(llm)
    # 生产路径 as_dispatch()：真实调用后经 _center 发 AFTER_MODEL。
    from runtime import Invoke
    result = asyncio.run(gw.as_dispatch()(
        Invoke(port="llm", payload={
            "messages": [{"role": "user", "content": "reply with OK only"}],
            "max_tokens": 5,
        }),
        PortCtx(session_id="s-deepseek")))
    assert result.ok and result.data, f"真实 LLM 应返回内容，实际 error={result.error!r}"
    assert "sk-" not in str(result), "响应不得含密钥"

    async def drain() -> list[object]:
        return [e async for e in stream.subscribe(STREAM_TOPIC)]
    events = asyncio.run(drain())
    assert any(getattr(e, "type", "") == "after_model" for e in events), (
        "真实调用后 after_model 应进事件流")
