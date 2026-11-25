"""RED：ModelEndpoint 密钥不进 repr；端点注册必须产出可解析的 LLMPort。

独立审计 EXP6-A / EXP8 证据：
- repr(ep) 直接打印 SUPER_SECRET_KEY。
- reg.register('backup', ...) 后 resolve 有值但 resolve_llm 为 None，
  gateway 走 TARGET_UNRESOLVED——注册了却永远不可用。
"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, LLMResponse, PortCtx
from harness.model.gateway import ModelGateway
from harness.model.registry import InMemoryModelRegistry, ModelEndpoint


class _StubLLM:
    def __init__(self, content: str) -> None:
        self.name = content
        self.content = content
        self.calls = 0
        self.last_usage = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content)

    def stream(self, req: LLMRequest, ctx: PortCtx):  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            if False:
                yield ""
        return gen()


def test_red_endpoint_repr_hides_api_key() -> None:
    ep = ModelEndpoint(name="p", base_url="http://x", api_key="SUPER_SECRET_KEY", model="m")
    assert "SUPER_SECRET_KEY" not in repr(ep), f"repr 泄露密钥：{repr(ep)}"
    assert "SUPER_SECRET_KEY" not in str(ep), f"str 泄露密钥：{str(ep)}"
    # 非敏感字段仍须可见，便于诊断。
    assert "p" in repr(ep) and "http://x" in repr(ep)
    assert ep.api_key == "SUPER_SECRET_KEY", "隐藏 repr 不得破坏字段读取"


def test_red_registered_endpoint_resolves_to_llm() -> None:
    reg = InMemoryModelRegistry()
    reg.register("backup", base_url="http://x", api_key="K", model="m")
    resolved = reg.resolve_llm("backup")
    assert resolved is not None, "注册端点必须同时注册可用后端，resolve_llm 不得为 None"
    assert reg.has("backup") is True
    endpoint = reg.resolve("backup")
    assert isinstance(endpoint, ModelEndpoint)
    assert endpoint.name == "backup"


def test_red_gateway_degrade_to_registered_endpoint_does_not_fall_back() -> None:
    """降级到已注册端点：必须切到该端点后端，绝不静默回退默认后端。"""
    async def run() -> None:
        backup = _StubLLM("backup-backend")
        reg = InMemoryModelRegistry(endpoint_llm_factory=lambda endpoint: backup)
        reg.register("backup", base_url="http://x", api_key="K", model="m")
        default = _StubLLM("default-backend")
        gw = ModelGateway(llm=default, registry=reg)
        result = await gw.complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
            PortCtx(), degrade_target="backup",
        )
        assert default.calls == 0, f"已注册端点不得回退默认后端，实际调用 {default.calls} 次"
        assert backup.calls == 1, f"已注册端点应被真实调用 1 次，实际 {backup.calls}"
        assert result.content == "backup-backend"
        assert result.error is None

    asyncio.run(run())
