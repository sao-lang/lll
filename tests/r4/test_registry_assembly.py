"""R4 装配：ModelCapability.install(container, reg) 统一注册表 —— 容器与网关同源。

P6：Registry 从旧 Host 迁出，由 AOPContainer / LifecycleBus / 装配层共用同一实例。
三项具名 provider 注册齐全、同名同实例幂等 / 同名异实例拒绝。
"""
from __future__ import annotations

import asyncio

import pytest
from compose import AOPContainer
from compose import HookBusError, LLMPort, LLMRequest, LLMResponse, PortCtx, Registry, Usage

from harness.model import InMemoryModelRegistry, ModelCapability, RuleRouter
from tests.r4.conftest import pctx


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


def _container() -> AOPContainer:
    return AOPContainer(lambda inv: __import__("runtime").Result.success("x"))


class TestRegistryAssembly:
    def test_install_sets_container_registry_same_instance_as_gateway(self) -> None:
        """install(container) 后 container.registry 与 gateway 解析用注册表同一实例。"""
        cap = ModelCapability(llm=FakeLLM("primary"))
        container = _container()
        returned = cap.install(container)
        assert returned is cap.registry
        assert container.registry is cap.registry
        assert cap.gateway().registry is container.registry

    def test_install_registers_llm_router_cache(self) -> None:
        """三项具名 provider 注册齐全。"""
        cap = ModelCapability(llm=FakeLLM("primary"), router=RuleRouter())
        reg = InMemoryModelRegistry()
        cap.install(_container(), reg)
        assert reg.has("llm") and reg.has("model_router") and reg.has("model_cache")
        assert reg.resolve("llm") is cap.llm

    def test_install_with_injected_registry_is_used(self) -> None:
        """注入 reg 时 gateway 复用注入实例，而非内部新建。"""
        cap = ModelCapability(llm=FakeLLM("primary"))
        reg = InMemoryModelRegistry()
        cap.install(_container(), reg)
        assert cap.gateway().registry is reg

    def test_gateway_degrades_via_shared_registry(self) -> None:
        """端到端：往共用注册表注册备用后端，gateway 按 typed target 切过去。"""
        cap = ModelCapability(llm=FakeLLM("primary"))
        container = _container()
        cap.install(container)
        backup = FakeLLM("backup")
        cap.add_backend("backup", backup)
        gw = cap.gateway()
        out = asyncio.run(gw.complete(
            LLMRequest(messages=[{"role": "user", "content": "hi"}]),
            pctx(), degrade_target="backup",
        ))
        assert out.content == "backup"


class TestRegistryIdempotency:
    def test_same_name_same_instance_is_noop(self) -> None:
        """同名同实例重复注册幂等。"""
        reg = InMemoryModelRegistry()
        llm = FakeLLM("primary")
        reg.register_llm("primary", llm)
        reg.register_llm("primary", llm)
        assert reg.resolve_llm("primary") is llm

    def test_same_name_different_instance_rejected(self) -> None:
        """同名不同实例拒绝（fail-fast）。"""
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", FakeLLM("a"))
        with pytest.raises(HookBusError, match="different instance"):
            reg.register_llm("primary", FakeLLM("b"))


class TestCostGuardWiring:
    def test_capability_injects_cost_guard_into_gateway(self) -> None:
        """ModelCapability 构造注入的 cost_guard 必须透传到 gateway（生产成本硬闸可用）。"""
        from harness.model import CostGuard

        guard = CostGuard(max_cost_usd=0.001)
        cap = ModelCapability(llm=FakeLLM("primary"), cost_guard=guard)
        gw = cap.gateway()
        # 行为断言：预算极低 + 预估超预算 → 调用前被硬闸拦截（非后端返回）。
        out = asyncio.run(gw.complete(
            LLMRequest(messages=[{"role": "user", "content": "hi"}]),
            pctx(), degrade_target=None,
        ))
        assert out.error is not None and "COST" in out.error.upper()
