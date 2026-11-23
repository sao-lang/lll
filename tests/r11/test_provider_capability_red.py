"""RED：通用 LLM provider 能力包（多厂商配置化 + 端点表真消费）。

现状：ModelRegistry 端点表（base_url/api_key/model）注册后无人构造适配器；
只有 OpenAICompatLLM 一个类，用户要自己 new 再 add_backend。"llm provider 是
最基础组件需要特别处理"——应有一层工厂协议 + 能力包，从配置产出可用 LLMPort。
"""
from __future__ import annotations

import httpx
import pytest

from compose import LLMRequest, LLMResponse, PortCtx, Usage
from harness.model import InMemoryModelRegistry, ModelCapability
from ports.adapters.llm import OpenAICompatLLM
from ports.adapters.provider import openai_http


class _FakeTransport(httpx.AsyncBaseTransport):
    """离线 HTTP 替身：记录请求并返回 OpenAI-compatible 响应。"""
    def __init__(self, model: str) -> None:
        self.model = model
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import json
        self.requests.append(request)
        body = {
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": self.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.model},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return httpx.Response(200, json=body, request=request)


def test_red_provider_capability_builds_provider_from_endpoint_config() -> None:
    """配置两个厂商端点 → 构造出两个可解析的 LLMPort（端点表真消费）。"""
    from ports.adapters.provider import LLMProviderCapability

    cap = LLMProviderCapability()
    cap.register("deepseek", factory=openai_http,
                 base_url="https://api.deepseek.com/v1", api_key="k1", model="deepseek-chat")
    cap.register("moonshot", factory=openai_http,
                 base_url="https://api.moonshot.cn/v1", api_key="k2", model="moonshot-v1")

    a = cap.resolve("deepseek")
    b = cap.resolve("moonshot")
    assert a is not None and b is not None
    assert a is not b
    assert a._base_url != b._base_url, "两个厂商端点不应共用同一实例"


def test_red_provider_capability_feeds_model_registry() -> None:
    """Provider 包产出的实例能登记进 ModelRegistry，被 ModelCapability 降级切换真消费。"""
    from ports.adapters.provider import LLMProviderCapability

    providers = LLMProviderCapability()
    deepseek = providers.register("deepseek", factory=openai_http,
                                  base_url="https://api.deepseek.com/v1",
                                  api_key="k1", model="deepseek-chat")
    backup = providers.register("backup", factory=openai_http,
                                base_url="https://backup.example/v1",
                                api_key="k2", model="backup-model")

    registry = InMemoryModelRegistry()
    providers.install_into(registry)
    assert registry.resolve_llm("deepseek") is deepseek
    assert registry.resolve_llm("backup") is backup

    cap = ModelCapability(llm=deepseek, registry=registry)
    assert cap.registry.resolve_llm("backup") is backup


def test_red_provider_capability_offline_complete_via_factory() -> None:
    """工厂产出的 provider 在离线 MockTransport 下真完成一次 LLM 调用。"""
    from ports.adapters.provider import LLMProviderCapability

    factory = lambda **kw: OpenAICompatLLM(client=httpx.AsyncClient(  # noqa: E731
        transport=_FakeTransport(kw["model"])), **kw)
    cap = LLMProviderCapability(default_factory=factory)
    cap.register("offline", base_url="https://offline.example/v1",
                 api_key="k", model="offline-model")
    llm = cap.resolve("offline")
    assert llm is not None
    resp = __import__("asyncio").run(llm.complete(
        LLMRequest(messages=[{"role": "user", "content": "hi"}]), PortCtx()))
    assert resp.content == "offline-model"
