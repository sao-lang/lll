"""R4 model 通知契约（R4-T12）：before/after_model 正常各一次，异常路径也发 after。

异常路径补发 after_model（payload 标 error）是 R4 详设 §5.1 的明确承诺；
本文件同时修复 R4-T12 门禁中原本指向不存在文件的幻影路径。
"""
from __future__ import annotations

import asyncio

import pytest
from compose.context import Invocation
from compose import LifecycleBus, LifecyclePhase, LLMRequest, LLMResponse, PortCtx, Usage
from runtime import Invoke
from harness.model import ModelGateway


class _Session:
    session_id = "s-notice"


class _Ctx:
    def __init__(self) -> None:
        self.session_id = "s-notice"
        self.meta: dict = {}


class _OkLLM:
    name = "ok"
    last_usage: Usage | None = None

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        return LLMResponse(content="done", usage=Usage())

    def stream(self, request: LLMRequest, ctx: PortCtx):
        async def gen():
            yield "done"
        return gen()


class _BoomLLM:
    name = "boom"
    last_usage: Usage | None = None

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        raise RuntimeError("backend exploded")

    def stream(self, request: LLMRequest, ctx: PortCtx):
        async def gen():
            yield ""
        return gen()


def _inv() -> Invocation:
    return Invocation(
        effect=Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]}),
        ctx=_Ctx(), session=_Session(), meta={},
    )


def test_normal_path_emits_before_and_after_exactly_once():
    """正常路径：before/after_model 各触发一次，after.payload.error 为空串。"""
    bus = LifecycleBus()
    seen: list[tuple[str, str]] = []

    def observe(notice) -> None:
        seen.append((notice.phase.value, str(notice.payload.get("error", ""))))

    bus.on(LifecyclePhase.BEFORE_MODEL, observe)
    bus.on(LifecyclePhase.AFTER_MODEL, observe)
    gw = ModelGateway(llm=_OkLLM(), bus=bus)

    asyncio.run(gw.as_call()(_inv()))

    assert [p for p, _ in seen] == ["before_model", "after_model"]
    assert seen[-1][1] == ""


def test_error_path_also_emits_after_model_with_error_payload():
    """异常路径：complete 抛错时 after_model 仍恰好发一次，payload.error 非空。"""
    bus = LifecycleBus()
    after: list[str] = []

    def observe(notice) -> None:
        if notice.phase is LifecyclePhase.AFTER_MODEL:
            after.append(str(notice.payload.get("error", "")))

    bus.on(LifecyclePhase.AFTER_MODEL, observe)
    gw = ModelGateway(llm=_BoomLLM(), bus=bus)

    with pytest.raises(RuntimeError, match="backend exploded"):
        asyncio.run(gw.as_call()(_inv()))

    assert len(after) == 1, "after_model 异常路径必须恰好发一次"
    assert after[0], "异常路径 after_model.payload.error 必须非空"


def test_backend_error_after_payload_carries_error():
    """后端返回 error（非异常）时，after_model.payload.error 等于该 error 文本。"""
    from compose import LLMResponse as R, Usage as U

    class _ErrLLM:
        name = "err"
        last_usage: U | None = None

        async def complete(self, request, ctx) -> R:
            return R(content="", error="BACKEND_FAIL: nope", usage=U())

        def stream(self, request, ctx):
            async def gen():
                yield ""
            return gen()

    bus = LifecycleBus()
    after: list[str] = []
    bus.on(LifecyclePhase.AFTER_MODEL,
           lambda n: after.append(str(n.payload.get("error", ""))))
    gw = ModelGateway(llm=_ErrLLM(), bus=bus)
    asyncio.run(gw.as_call()(_inv()))
    assert len(after) == 1 and after[0] == "BACKEND_FAIL: nope"


def test_refuse_after_payload_marked_refused():
    """REFUSE 拒答也发 after_model（恰好一次），error 标 REFUSED 以便与成功区分。"""
    from compose.context import RoutingTarget

    bus = LifecycleBus()
    after: list[str] = []
    bus.on(LifecyclePhase.AFTER_MODEL,
           lambda n: after.append(str(n.payload.get("error", ""))))
    gw = ModelGateway(llm=_OkLLM(), bus=bus)
    inv = _inv()
    inv.write_routing_target(RoutingTarget(target="refuse", reason="test"))
    asyncio.run(gw.as_call()(inv))
    assert len(after) == 1 and after[0] == "REFUSED"


def test_cancelled_error_still_emits_after_and_reraises():
    """CancelledError：after 仍发一次，且原异常原样抛穿（finally 不覆盖取消语义）。"""
    import asyncio as aio

    class _CancelLLM:
        name = "cancel"
        last_usage: Usage | None = None

        async def complete(self, request, ctx) -> LLMResponse:
            raise aio.CancelledError()

        def stream(self, request, ctx):
            async def gen():
                yield ""
            return gen()

    bus = LifecycleBus()
    after: list[str] = []
    bus.on(LifecyclePhase.AFTER_MODEL,
           lambda n: after.append(str(n.payload.get("error", ""))))
    gw = ModelGateway(llm=_CancelLLM(), bus=bus)
    with pytest.raises(aio.CancelledError):
        asyncio.run(gw.as_call()(_inv()))
    assert len(after) == 1 and "CancelledError" in after[0]
