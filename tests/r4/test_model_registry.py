"""R4 降级/路由链测试：端点注册表 + router 真调用 + 降级切端点。

新契约：gateway.as_call() 从 inv.meta 的 typed routing_target（Degradation 写入）
读降级目标；REFUSE 直接产出 Result.failure("REFUSED")，零后端调用；
未注册目标抛错，绝不静默回退。
"""
from __future__ import annotations

import asyncio

from compose import LLMPort, LLMRequest, LLMResponse, PortCtx, Usage
from compose.context import Invocation, RoutingTarget
from runtime import Invoke

from harness.model import InMemoryModelRegistry, ModelGateway, ModelRegistry, RouteDecision
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

    def __init__(self, target: str) -> None:
        self.target = target
        self.calls = 0

    def route(self, effect: object, ctx: object) -> object:
        self.calls += 1
        return RouteDecision(model=self.target, reason="spy")


class _SessionView:
    session_id = "s-r4"


def _ctx(*, target: str | None = None) -> Invocation:
    """构造 Invocation；target 非空时写 typed routing_target（替代旧裸桥接字段）。"""
    inv = Invocation(
        effect=Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]}),
        ctx=type("Ctx", (), {"session_id": "s-r4", "meta": {}})(),
        session=_SessionView(),
        meta={},
    )
    if target is not None:
        inv.write_routing_target(RoutingTarget(target=target, reason="test"))
    return inv


def _effect() -> Invoke:
    return Invoke(port="llm", payload={'messages': [{'role': 'user', 'content': 'hi'}]})


class TestModelRegistry:
    def test_register_resolve(self) -> None:
        reg = InMemoryModelRegistry()
        reg.register("primary", base_url="https://a/openai", api_key="k1", model="gpt-a")
        ep = reg.resolve("primary")
        assert ep is not None
        assert ep.base_url == "https://a/openai" and ep.model == "gpt-a"

    def test_resolve_missing_returns_none(self) -> None:
        assert InMemoryModelRegistry().resolve("nope") is None

    def test_register_llm_resolves_instance(self) -> None:
        llm = FakeLLM("primary")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", llm)
        assert reg.resolve_llm("primary") is llm
        assert reg.resolve_llm("nope") is None

    def test_implements_protocol(self) -> None:
        assert isinstance(InMemoryModelRegistry(), ModelRegistry)


class TestRouterActuallyCalled:
    def test_gateway_invokes_router(self) -> None:
        spy = SpyRouter("primary")
        gw = ModelGateway(llm=FakeLLM("fallback"), router=spy)  # type: ignore[arg-type]
        asyncio.run(gw.complete(req(), pctx()))
        assert spy.calls >= 1, "router.route 从未被调用（_router 空转未修复）"

    def test_gateway_without_registry_uses_default(self) -> None:
        """无降级目标时仍用默认 llm（未传 registry 的兼容行为）。"""
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
        r = asyncio.run(gw.complete(req(), pctx(), degrade_target="backup"))
        assert r.content == "backup", "降级 target 未切到备用端点"
        assert backup.calls == 1 and primary.calls == 0

    def test_unknown_target_yields_target_unresolved_no_silent_fallback(self) -> None:
        """未注册降级目标 → TARGET_UNRESOLVED 显式失败，绝不静默回退默认模型（红线）。

        本契约三态设计取代了旧的异常折叠——
        TargetUnresolved.to_result() 产出稳定码 TARGET_UNRESOLVED（见 test_refuse.py）。
        """
        primary = FakeLLM("primary")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        gw = ModelGateway(llm=primary, registry=reg)
        result = asyncio.run(gw.as_call()(_ctx(target="ghost")))
        assert not result.ok
        assert result.error is not None and result.error.code == "TARGET_UNRESOLVED"
        assert primary.calls == 0


class TestGatewayAsCallAdapter:
    def test_as_call_reads_target_from_scope_bridge(self) -> None:
        """as_call 从 inv.meta 的 typed routing_target 读降级目标并切到注册后端。"""
        primary = FakeLLM("primary")
        backup = FakeLLM("backup")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        reg.register_llm("backup", backup)
        gw = ModelGateway(llm=primary, registry=reg)

        result = asyncio.run(gw.as_call()(_ctx(target="backup")))
        assert result.ok and result.data["content"] == "backup"
        assert backup.calls == 1 and primary.calls == 0

    def test_as_call_refuse_fails_without_backend_call(self) -> None:
        """scope.target='refuse' → Result.failure('REFUSED')，零后端调用、零回退。"""
        primary = FakeLLM("primary")
        backup = FakeLLM("backup")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        reg.register_llm("backup", backup)
        gw = ModelGateway(llm=primary, registry=reg)

        result = asyncio.run(gw.as_call()(_ctx(target="refuse")))
        assert not result.ok
        assert result.error is not None and result.error.code == "REFUSED"
        assert primary.calls == 0 and backup.calls == 0

    def test_as_call_without_bridge_uses_default(self) -> None:
        """未桥接 scope 时无降级目标：走默认后端。"""
        primary = FakeLLM("primary")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        gw = ModelGateway(llm=primary, registry=reg)

        result = asyncio.run(gw.as_call()(_ctx()))
        assert result.ok and result.data["content"] == "primary"
        assert primary.calls == 1
