"""R4 降级/路由链测试（v1.5）：端点注册表 + router 真调用 + DEGRADE 切端点。

修复三处断点：DEGRADE 无消费者 / _router 空转 / 无模型名→端点注册。
用 fake LLMPort 验证按端点实例切换（不发真 HTTP）。
"""
from __future__ import annotations

import asyncio

from hooks import LLMPort, LLMRequest, LLMResponse, PortCtx, Usage

from harness.model import InMemoryModelRegistry, ModelGateway, ModelRegistry
from tests.r4.conftest import pctx, req


class FakeLLM:
    """按名字区分的假后端，验证请求真的落到对应实例。"""
    name: str
    last_usage = Usage()

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.name, model=self.name, usage=Usage())

    def stream(self, request: LLMRequest, ctx: PortCtx):
        async def gen():
            yield self.name
        return gen()


class SpyRouter:
    """记录 route 是否真被调用（验证 _router 不再空转）。"""
    name = "spy_router"
    calls = 0

    def __init__(self, target: str) -> None:
        self.target = target

    def route(self, effect: object, ctx: object) -> object:
        from harness.model import RouteDecision
        self.calls += 1
        return RouteDecision(model=self.target, reason="spy")


class TestModelRegistry:
    def test_register_resolve(self) -> None:
        reg = InMemoryModelRegistry()
        reg.register("primary", base_url="https://a/v1", api_key="k1", model="gpt-a")
        ep = reg.resolve("primary")
        assert ep is not None
        assert ep.base_url == "https://a/v1" and ep.model == "gpt-a"

    def test_resolve_missing_returns_none(self) -> None:
        assert InMemoryModelRegistry().resolve("nope") is None

    def test_implements_protocol(self) -> None:
        assert isinstance(InMemoryModelRegistry(), ModelRegistry)


class TestRouterActuallyCalled:
    def test_gateway_invokes_router(self) -> None:
        spy = SpyRouter("primary")
        gw = ModelGateway(llm=FakeLLM("fallback"), router=spy)  # type: ignore[arg-type]
        asyncio.run(gw.complete(req(), pctx()))
        assert spy.calls >= 1, "router.route 从未被调用（_router 空转未修复）"

    def test_gateway_without_registry_keeps_behavior(self) -> None:
        """兼容：无注册表时仍用默认 llm，行为与旧版一致。"""
        llm = FakeLLM("default")
        gw = ModelGateway(llm=llm)
        r = asyncio.run(gw.complete(req(), pctx()))
        assert r.content == "default" and llm.calls == 1


class TestDegradeTargetSwitchesEndpoint:
    def test_degrade_target_picks_registered_llm(self) -> None:
        primary = FakeLLM("primary")
        backup = FakeLLM("backup")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        reg.register_llm("backup", backup)
        gw = ModelGateway(llm=primary, registry=reg)
        # 降级目标为 backup：gateway 应切到 backup 实例
        r = asyncio.run(gw.complete(req(), pctx(), degrade_target="backup"))
        assert r.content == "backup", "DEGRADE target 未切到备用端点"
        assert backup.calls == 1 and primary.calls == 0

    def test_unknown_target_falls_back(self) -> None:
        """端点未注册时回退默认后端（不崩、进 trace）。"""
        primary = FakeLLM("primary")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        gw = ModelGateway(llm=primary, registry=reg)
        r = asyncio.run(gw.complete(req(), pctx(), degrade_target="ghost"))
        assert r.content == "primary" and primary.calls == 1


class TestGatewayAsCallAdapter:
    def test_as_call_reads_degrade_target_from_ctx(self) -> None:
        """gateway.as_call() 从 ctx.meta 读 bus.dispatch 写入的 degrade_target 并切端点。"""
        from hooks import HookCtx
        from runtime import AgentState, Effect, EffectKind

        primary = FakeLLM("primary")
        backup = FakeLLM("backup")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        reg.register_llm("backup", backup)
        gw = ModelGateway(llm=primary, registry=reg)

        ctx = HookCtx(
            effect=Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]}),
            state=AgentState(), session_id="s", event="before_invoke",
        )
        ctx.meta["degrade_target"] = "backup"  # bus.dispatch() 写入的接力字段
        result = asyncio.run(gw.as_call()(
            Effect(EffectKind.LLM, {"messages": [{"role": "user", "content": "hi"}]}), ctx
        ))
        assert result.ok and result.data["content"] == "backup"
        assert backup.calls == 1 and primary.calls == 0
