"""AOP 五角色增强器协议（独立命名，与 NestJS 切开）。

角色（how 维度，返回值被框架消费）：
    Prelude  前导（最外层包裹，类比 Middleware）
    Warden   守卫（准入，Verdict(allowed, reason)）
    Envelope 环绕（Interceptor，包裹中心调用）
    Refiner  精炼（Pipe，改写 Invocation）
    Salvage  兜底（ExceptionFilter，异常/失败出口）

执行顺序框架固定，增强器不声明 priority：
    Prelude → Warden → Envelope(前) → Refiner → Port call → Envelope(后) → Salvage(异常/失败时)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from runtime import Result

from .invocation import Invocation, Next, Verdict

__all__ = [
    "Prelude",
    "Warden",
    "Envelope",
    "Refiner",
    "Salvage",
    "FailureClass",
    "ROLE_NAMES",
]


@dataclass(frozen=True)
class FailureClass:
    """失败分类（R3 重试/熔断判定结果）。

    字段：
        code: 失败类别码（如 retryable/terminal）。
        retryable: 是否可重试。
        suggested_delay: 建议退避秒数（服务端 Retry-After）。
    """
    code: str
    retryable: bool = False
    suggested_delay: float | None = None


@runtime_checkable
class Prelude(Protocol):
    """前导：最外层包裹整条增强链 + 中心调用。"""
    name: str

    async def run(self, inv: Invocation, next: Next) -> Result: ...


@runtime_checkable
class Warden(Protocol):
    """守卫：准入判定。allowed=False 短路拒绝；降级意图写 inv.meta 故障记录。"""
    name: str

    async def guard(self, inv: Invocation) -> Verdict: ...


@runtime_checkable
class Envelope(Protocol):
    """环绕：包裹中心调用（前置/后置/异常兜底由实现自行 try/finally）。"""
    name: str

    async def wrap(self, inv: Invocation, next: Next) -> Result: ...


@runtime_checkable
class Refiner(Protocol):
    """精炼：转换 Invocation，返回新的调用上下文（不原地改写）。"""
    name: str

    async def refine(self, inv: Invocation) -> Invocation: ...


@runtime_checkable
class Salvage(Protocol):
    """兜底：异常/失败出口，返回兜底 Result（可写 typed 降级目标供网关消费）。"""
    name: str

    async def recover(self, exc: Exception, inv: Invocation) -> Result: ...


ROLE_NAMES = ("prelude", "warden", "envelope", "refiner", "salvage")
"""五角色注册名（AOPContainer 内部分组键）。"""
