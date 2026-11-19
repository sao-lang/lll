"""R4-T08（真实覆盖，替代幻影 tests/r4/test_registry_assembly.py）：
ModelCapability.install(host, reg) 统一注册表 —— gateway 与 host.registry 同源、
llm/router/cache 三项注册齐全、同名同实例幂等 / 同名异实例拒绝。

设计出处：R4-模型与推理.md §2.1 断点一 / §5.3 ModelRegistry / §5.6 install / §6 两表统一。
"""
from __future__ import annotations

import asyncio

import pytest
from hooks import HookBusError, Host, LLMPort, LLMRequest, LLMResponse, PortCtx, Usage

from harness.model import (
    InMemoryModelRegistry,
    ModelCapability,
    ModelGateway,
    RuleRouter,
)


class FakeLLM:
    name = "fake"
    last_usage = Usage()

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        return LLMResponse(content=self.name, usage=Usage())

    def stream(self, request: LLMRequest, ctx: PortCtx):
        async def gen():
            yield self.name
        return gen()


class TestRegistryAssembly:
    def test_install_sets_host_registry_same_instance_as_gateway(self) -> None:
        """install(host) 后 host.registry 与 gateway 解析用注册表是同一实例。"""
        cap = ModelCapability(llm=FakeLLM("primary"))
        host = Host()
        returned = cap.install(host)
        assert returned is host
        assert host.registry is not None
        gw = cap.gateway()
        assert gw.registry is host.registry, "gateway 与 host.registry 未同源"

    def test_install_registers_llm_router_cache(self) -> None:
        """三项具名 provider 注册齐全（同名同实例幂等）。"""
        cap = ModelCapability(llm=FakeLLM("primary"), router=RuleRouter())
        reg = InMemoryModelRegistry()
        host = Host()
        cap.install(host, reg)
        assert reg.has("llm") and reg.has("model_router") and reg.has("model_cache")
        assert reg.resolve("llm") is cap.llm

    def test_install_with_injected_registry_is_used(self) -> None:
        """注入 reg 时 gateway 复用注入实例，而非内部新建。"""
        cap = ModelCapability(llm=FakeLLM("primary"))
        reg = InMemoryModelRegistry()
        cap.install(Host(), reg)
        assert cap.gateway().registry is reg

    def test_gateway_degrades_via_host_registry(self) -> None:
        """端到端：install 后往 host.registry 注册备用后端，gateway 按 target 切过去。"""
        primary = FakeLLM("primary")
        cap = ModelCapability(llm=primary)
        host = Host()
        cap.install(host)
        backup = FakeLLM("backup")
        assert host.registry is not None
        host.registry.register_llm("backup", backup)  # type: ignore[union-attr]
        gw = cap.gateway()
        out = asyncio.run(gw.complete(
            LLMRequest(messages=[{"role": "user", "content": "hi"}]),
            _pctx(), degrade_target="backup",
        ))
        assert out.content == "backup"


class TestRegistryIdempotency:
    def test_same_name_same_instance_is_noop(self) -> None:
        """同名同实例重复注册幂等（不抛异常）。"""
        reg = InMemoryModelRegistry()
        llm = FakeLLM("primary")
        reg.register_llm("primary", llm)
        reg.register_llm("primary", llm)  # 幂等
        assert reg.resolve_llm("primary") is llm

    def test_same_name_different_instance_rejected(self) -> None:
        """同名不同实例拒绝（fail-fast，防静默覆盖）。"""
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", FakeLLM("a"))
        with pytest.raises(HookBusError, match="different instance"):
            reg.register_llm("primary", FakeLLM("b"))


def _pctx():
    from tests.r4.conftest import pctx
    return pctx()
