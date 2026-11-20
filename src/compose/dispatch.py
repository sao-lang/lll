"""dispatch（规格 §2.5）：按 effect.port 分派到 Port，构造 Result（含 Usage）。

最小职责：async (effect, ctx) -> Result；不构造 Invocation（R1 明确不构造）。
typed target 消费仅降级场景需要，由②降级守卫 + ③装饰器协作完成，dispatch 不感知。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from runtime import CallFn, DynamicValue, Effect, Invoke, PortError, Result

__all__ = ["dispatch", "make_dispatch"]


# Port 分派器签名：(effect, ctx) -> Result；由各能力模块（R4/R5/R6/R7）提供。
PortMap = Mapping[str, Callable[[Invoke, object], Result]]


def make_dispatch(ports: PortMap) -> CallFn:
    """构造 dispatch：按 effect.port 查 Port 处理器并调用，异常折叠为 Result。

    Args:
        ports: port 名 → 处理器（同步返回 Result；异步由 CallFn 层 await）。

    Returns:
        符合 R1 CallFn 签名的 dispatch 函数。
    """
    async def call(effect: Effect, ctx: object) -> Result:
        # 分支：非 Invoke 不进 dispatch（Spawn/Suspend/Terminate 走 R1 快路径，D-7）。
        if not isinstance(effect, Invoke):
            return Result.failure("INVALID_EFFECT", "dispatch only handles Invoke")
        handler = ports.get(effect.port)
        if handler is None:
            return Result.failure("PORT_MISSING", f"port not registered: {effect.port}")
        try:
            return handler(effect, ctx)
        except Exception as exc:  # noqa: BLE001 - Port 异常统一折叠，不抛穿 R1
            return Result.failure("PORT_CRASHED", f"{type(exc).__name__}: {exc}")

    return call


async def dispatch(ports: PortMap, effect: Effect, ctx: object) -> Result:
    """便捷 dispatch：直接按 ports 分派单个 Effect（make_dispatch 的无封装形态）。"""
    return await make_dispatch(ports)(effect, ctx)
