"""③call 装饰器 + build_call + dispatch 的语义单测（规格 §2.4/§2.5）。

覆盖：call 装饰器只覆盖 Invoke、build_call 装饰器组合顺序、
dispatch 按 port 分派构造 Result。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime import CallFn, Invoke, Result, Usage

from compose.call import (
    build_call,
    danger_route,
    injection_guard,
    retry_call,
    timeout,
)
from compose.dispatch import make_dispatch


def _base() -> CallFn:
    async def call(effect: object, ctx: object) -> Result:
        return Result.success("ok", usage=Usage(prompt_tokens=1, completion_tokens=1))
    return call


def test_retry_call_retries_retryable() -> None:
    """③retry 调用级：error.retryable 时重试，成功即返回。"""
    attempts = 0
    async def flaky(effect: object, ctx: object) -> Result:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return Result.failure("500", "down", retryable=True)
        return Result.success("ok")
    result = asyncio.run(retry_call(flaky, attempts=5, backoff=0)(Invoke(port="llm"), object()))
    assert result.ok and result.data == "ok"
    assert attempts == 3


def test_retry_call_no_retry_on_non_retryable() -> None:
    """③retry：非 retryable 失败不重试。"""
    calls = 0
    async def fail(effect: object, ctx: object) -> Result:
        nonlocal calls
        calls += 1
        return Result.failure("400", "bad", retryable=False)
    asyncio.run(retry_call(fail, attempts=5, backoff=0)(Invoke(port="llm"), object()))
    assert calls == 1


def test_build_call_decorators_apply_in_order() -> None:
    """build_call：装饰器按序包裹 base，最后注册者在最外层、最先执行。"""
    order: list[str] = []

    def mark(name: str):
        def deco(base: CallFn) -> CallFn:
            async def call(effect: object, ctx: object) -> Result:
                order.append(name)
                return await base(effect, ctx)
            return call
        return deco

    call = build_call(_base(), decorators=[mark("first"), mark("second")])
    result = asyncio.run(call(Invoke(port="llm"), object()))
    assert result.ok
    assert order == ["second", "first"]  # decorators[-1] 最外层先执行


def test_dispatch_routes_by_port() -> None:
    """dispatch：按 effect.port 分派到对应 Port 处理器，构造 Result。"""
    def llm_handler(effect: Invoke, ctx: object) -> Result:
        return Result.success("llm-ok")
    call = make_dispatch({"llm": llm_handler})
    result = asyncio.run(call(Invoke(port="llm"), object()))
    assert result.ok and result.data == "llm-ok"


def test_dispatch_missing_port() -> None:
    """dispatch：未注册 Port 返回 PORT_MISSING 失败信封，不抛异常。"""
    call = make_dispatch({})
    result = asyncio.run(call(Invoke(port="unknown"), object()))
    assert not result.ok and result.error is not None
    assert result.error.code == "PORT_MISSING"


def test_dispatch_ignores_non_invoke() -> None:
    """dispatch：只处理 Invoke，其余 Effect 由 R1 快路径（call 拦不到，D-7）。"""
    from runtime import Terminate
    call = make_dispatch({})
    result = asyncio.run(call(Terminate(output="x"), object()))
    assert not result.ok and result.error is not None
    assert result.error.code == "INVALID_EFFECT"


def test_danger_route_and_injection_guard_compose() -> None:
    """③装饰器函数组合：danger_route + injection_guard 可链式包裹（replace AOP）。"""
    class _Detector:
        def detect(self, payload: object, ctx: object) -> object:
            class V: allowed = True
            return V()
    class _Registry:
        def resolve(self, name: str) -> object:
            return None
    call = danger_route(injection_guard(_base(), _Detector()), _Registry())
    result = asyncio.run(call(Invoke(port="tool", payload={"danger": "low"}), object()))
    assert result.ok
