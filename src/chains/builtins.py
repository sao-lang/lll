"""M2 内置示例组件：演示 M8/M9/M7 如何"实现接口 + 挂到链路"接入。

全部带 name（透明性红线）；全部无状态或只收集观测数据（不持有会话状态）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Literal, cast

from runtime import Result

from .types import ChainCtx, ChainEvent, Verdict

__all__ = [
    "AddMetaPipe", "RedactPipe", "PrefixPipe", "RejectFilter", "AllowFilter",
    "RetryFilter", "FallbackFilter", "DegradeFilter", "AbortFilter",
    "RedactResultFilter", "TimeoutMiddleware", "PassMiddleware",
    "ShortCircuitMiddleware", "NeedsPortMiddleware", "TracingHook", "BoomHook",
]


class AddMetaPipe:
    """去程 pipe：给 payload 加一个键（示例：上下文规范化）。"""

    def __init__(self, name: str, key: str, value: Any) -> None:
        self.name, self.key, self.value = name, key, value

    def transform(self, data: Any, ctx: ChainCtx) -> Any:
        if isinstance(data, dict):
            return {**data, self.key: self.value}
        return data


class RedactPipe:
    """去程 pipe：脱敏 payload 指定键（M8 PII 清洗的形态）。"""

    def __init__(self, name: str, key: str, replacement: str = "[REDACTED]") -> None:
        self.name, self.key, self.replacement = name, key, replacement

    def transform(self, data: Any, ctx: ChainCtx) -> Any:
        if isinstance(data, dict) and self.key in data:
            return {**data, self.key: self.replacement}
        return data


class PrefixPipe:
    """回程 pipe：给字符串结果加前缀（示例大结果裁剪/变换）。"""

    def __init__(self, name: str, prefix: str = "«") -> None:
        self.name, self.prefix = name, prefix

    def transform(self, data: Any, ctx: ChainCtx) -> Any:
        return f"{self.prefix}{data}" if isinstance(data, str) else data


class AllowFilter:
    def __init__(self, name: str = "allow-filter") -> None:
        self.name = name

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        return Verdict.allow()


class RejectFilter:
    """去程护栏：命中关键词即 reject（首个 reject 生效，后续 filter 与 port 均不执行）。"""

    def __init__(self, name: str, keyword: str, *, on_response: bool = False) -> None:
        self.name, self.keyword, self.on_response = name, keyword, on_response

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        if phase == "request" and not self.on_response:
            if self.keyword in str(dict(ctx.effect.payload)):
                return Verdict.reject(f"blocked keyword: {self.keyword}")
        elif phase == "response" and self.on_response:
            if self.keyword in str(ctx.meta.get("error")):
                return Verdict.reject(f"blocked response: {self.keyword}")
        return Verdict.allow()


class RetryFilter:
    """回程错误判决：Port 失败时 retry(n, backoff)（计数在 ctx.attempt）。"""

    def __init__(self, name: str, times: int = 2, backoff: float = 0.0) -> None:
        self.name, self.times, self.backoff = name, times, backoff

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("retry", retry=self.times, backoff=self.backoff, reason="retry port error")
        return Verdict.allow()


class FallbackFilter:
    def __init__(self, name: str, fallback: Any) -> None:
        self.name, self.fallback = name, fallback

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("skip", data=self.fallback, reason="fallback applied")
        return Verdict.allow()


class DegradeFilter:
    def __init__(self, name: str, port: str) -> None:
        self.name, self.port = name, port

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("degrade", data=self.port, reason="switch to backup port")
        return Verdict.allow()


class AbortFilter:
    def __init__(self, name: str) -> None:
        self.name = name

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("abort", reason="abort on port error")
        return Verdict.allow()


class RedactResultFilter:
    def __init__(self, name: str, data: Any) -> None:
        self.name, self.data = name, data

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        return Verdict("redact", data=self.data, reason="redacted by filter") if phase == "response" else Verdict.allow()


class TimeoutMiddleware:
    def __init__(self, name: str, seconds: float) -> None:
        self.name, self.seconds = name, seconds

    async def __call__(self, ctx: ChainCtx, nxt: Callable[..., Any]) -> Result:
        return await asyncio.wait_for(nxt(), timeout=self.seconds)


class PassMiddleware:
    """调 next 但不返回：框架应透传内层结果。"""

    def __init__(self, name: str = "pass-mw") -> None:
        self.name = name

    async def __call__(self, ctx: ChainCtx, nxt: Callable[..., Any]) -> None:
        await nxt()


class ShortCircuitMiddleware:
    """未声明短路的红线样本：不调 next（需注册时 allow_short_circuit=True）。"""

    def __init__(self, name: str = "short-circuit-mw") -> None:
        self.name = name

    async def __call__(self, ctx: ChainCtx, nxt: Callable[..., Any]) -> Result:
        return Result.success("short-circuited")


class NeedsPortMiddleware:
    """声明 port 依赖，供 freeze() 装配期检查。"""

    def __init__(self, name: str = "needs-llm", requires_ports: tuple[str, ...] = ("llm",)) -> None:
        self.name = name
        self.requires_ports = requires_ports

    async def __call__(self, ctx: ChainCtx, nxt: Callable[..., Any]) -> Result:
        return cast(Result, await nxt())


class TracingHook:
    """只读观察：收集 ChainEvent（M9 TelemetryHook 的最小形态）。"""

    def __init__(self, name: str = "tracing-hook") -> None:
        self.name = name
        self.events: list[ChainEvent] = []

    async def on_event(self, event: ChainEvent, ctx: ChainCtx) -> None:
        self.events.append(event)


class BoomHook:
    """故障注入：异常必须被吞掉且不影响主流程（M2-G2 隔离用例）。"""

    def __init__(self, name: str = "boom-hook") -> None:
        self.name = name

    async def on_event(self, event: ChainEvent, ctx: ChainCtx) -> None:
        raise RuntimeError("observer failed")
