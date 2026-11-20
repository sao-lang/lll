"""R4-T09/T10（真实覆盖，替代幻影 tests/r4/test_refuse.py）：
REFUSE 真拒答优先于缓存 + 未注册 target 显式 TARGET_UNRESOLVED，任何分支零回退。

设计出处：R4-模型与推理.md §2.2 三态解析 / §5.4 BackendResolution / §7.1 拒答优先于缓存。
"""
from __future__ import annotations

import asyncio

from compose import LLMRequest, LLMResponse, PortCtx, Usage
from compose.context import Invocation, RoutingTarget
from runtime import Invoke

from harness.model import (
    BackendResolution,
    InMemoryModelRegistry,
    ModelGateway,
    RefuseExecution,
    TargetUnresolved,
)
from tests.r4.conftest import pctx


class CountingLLM:
    """按名字区分的假后端，记录调用次数以证明零回退。"""

    last_usage = Usage()

    def __init__(self, name: str = "primary") -> None:
        self.name = name
        self.calls = 0

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.name, usage=Usage())

    def stream(self, request: LLMRequest, ctx: PortCtx):
        async def gen():
            self.calls += 1
            yield self.name
        return gen()


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


class TestBackendResolutionTypes:
    def test_three_state_types_exist_and_fold(self) -> None:
        """三态载体存在且 to_result 折叠为稳定错误码。"""
        refuse = RefuseExecution(reason="ladder exhausted")
        unresolved = TargetUnresolved(target="ghost")
        assert refuse.to_result().error is not None
        assert refuse.to_result().error.code == "REFUSED"  # type: ignore[union-attr]
        assert unresolved.to_result().error is not None
        assert unresolved.to_result().error.code == "TARGET_UNRESOLVED"  # type: ignore[union-attr]
        # 类型别名可注解
        resolution: BackendResolution = refuse
        assert isinstance(resolution, RefuseExecution)


class TestRefusePriorityOverCache:
    def test_refuse_beats_cached_response(self) -> None:
        """缓存已有条目时 refuse 仍优先：返回 REFUSED、不返回缓存、后端零追加调用。"""
        primary = CountingLLM()
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        gw = ModelGateway(llm=primary, registry=reg)
        # 先塞一条缓存（正常调用一次）
        asyncio.run(gw.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}]), pctx()))
        assert primary.calls == 1

        result = asyncio.run(gw.as_call()(_ctx(target="refuse")))
        assert not result.ok
        assert result.error is not None and result.error.code == "REFUSED"
        assert primary.calls == 1, "拒答路径不应再调后端，也不应返回缓存内容"

    def test_refuse_direct_complete_folds_code(self) -> None:
        """direct complete 也走唯一决策点：refuse 折叠为带 REFUSED 的 error 响应。"""
        primary = CountingLLM()
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        gw = ModelGateway(llm=primary, registry=reg)
        resp = asyncio.run(gw.complete(req(), pctx(), degrade_target="refuse"))
        assert resp.error is not None and resp.error.startswith("REFUSED")
        assert primary.calls == 0


def req() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hi"}])


class TestTargetUnresolved:
    def test_unknown_target_yields_target_unresolved_no_backend(self) -> None:
        """非空未注册 target → TARGET_UNRESOLVED 失败，默认/备用后端均零调用。"""
        primary = CountingLLM()
        backup = CountingLLM("backup")
        reg = InMemoryModelRegistry()
        reg.register_llm("primary", primary)
        reg.register_llm("backup", backup)
        gw = ModelGateway(llm=primary, registry=reg)

        result = asyncio.run(gw.as_call()(_ctx(target="ghost")))
        assert not result.ok
        assert result.error is not None and result.error.code == "TARGET_UNRESOLVED"
        assert primary.calls == 0 and backup.calls == 0

    def test_no_registry_still_lands_default_when_no_target(self) -> None:
        """无 target 时仍走默认后端（与注册表存在与否无关的兼容路径）。"""
        primary = CountingLLM()
        gw = ModelGateway(llm=primary)
        result = asyncio.run(gw.as_call()(_ctx()))
        assert result.ok and result.data["content"] == "primary"
        assert primary.calls == 1
