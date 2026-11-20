"""插件 → 组合层原语的适配器（规格 P2/P3，修复 B1 生产接线）。

把既有韧性插件（其方法签名为 guard(inv)/wrap(inv,next)/observe(inv,result)）
适配为组合层的 ②Guard 与 ③call 装饰器，使生产 call 链真实经过
`build_call` / `with_guard` / `CallFn` 组合，而非停留在旧角色槽位。

不改动插件的领域逻辑（四坑/三态机/退避原样），只做签名桥接。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from runtime import Effect, Result

from .call import CallFn, Guard, Verdict, with_guard
from .context import CallContext, Invocation, Next

__all__ = ["WardenGuardAdapter", "envelope_decorator"]


class WardenGuardAdapter:
    """把 ②守卫插件（guard(inv)→Verdict）适配为 compose.Guard 协议。

    check：调插件 guard(inv)，Verdict 原样透传；
    observe：插件若有 observe(inv, result) 则转发（如 CircuitBreaker 后置记数）。
    """
    def __init__(self, plugin: object) -> None:
        """绑定插件实例（保留其跨调用状态）。"""
        self._plugin = plugin
        self.name = str(getattr(plugin, "name", "warden_adapter"))

    async def check(self, effect: Effect, ctx: object) -> Verdict | None:
        """调插件 guard，把结果归一为 compose.Verdict。"""
        guard = getattr(self._plugin, "guard", None)
        if not callable(guard):
            return None
        inv = _as_invocation(effect, ctx)
        verdict = await _maybe_await(guard(inv))
        if verdict is None:
            return None
        allowed = bool(getattr(verdict, "allowed", True))
        reason = str(getattr(verdict, "reason", ""))
        return Verdict(allowed=allowed, reason=reason)

    async def observe(self, effect: Effect, result: Result, ctx: object) -> None:
        """转发插件 observe(inv, result)（若有）。"""
        observe = getattr(self._plugin, "observe", None)
        if callable(observe):
            inv = _as_invocation(effect, ctx)
            await _maybe_await(observe(inv, result))


def envelope_decorator(plugin: object) -> Callable[[CallFn], CallFn]:
    """把 ③环绕插件（wrap(inv, next)→Result）适配为 call 装饰器（CallFn→CallFn）。

    桥接：Next(inv) 与 CallFn(effect, ctx) 互相转换，共享同一 Invocation 上下文。
    """
    def decorate(base: CallFn) -> CallFn:
        async def call(effect: Effect, ctx: object) -> Result:
            inv = _as_invocation(effect, ctx)

            async def inner(i: Invocation) -> Result:
                # 传 Invocation 本身作为 ctx（base 能识别并复用其 meta/typed target），
                # 不能传 i.ctx（原始 RuntimeCtx 丢失 typed target 导致降级重路由失效）。
                return await base(i.effect, i)

            wrap = getattr(plugin, "wrap")
            return cast(Result, await _maybe_await(wrap(inv, inner)))
        return call
    return decorate


def _as_invocation(effect: Effect, ctx: object) -> Invocation:
    """由 (effect, ctx) 构造 Invocation；若 ctx 已是 Invocation 则原样复用（共享 meta）。"""
    if isinstance(ctx, Invocation):
        return ctx
    session = type("_S", (), {"session_id": str(getattr(ctx, "session_id", "") or "")})()
    ctx_meta = getattr(ctx, "meta", None)
    meta = ctx_meta if isinstance(ctx_meta, dict) else {}
    return Invocation(effect=effect, ctx=ctx, session=session, meta=meta)


async def _maybe_await(value: Any) -> Any:
    """统一等待同步/异步返回。"""
    import inspect
    return await value if inspect.isawaitable(value) else value
