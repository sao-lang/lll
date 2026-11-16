"""M2 内置示例组件：演示 M8/M9/M7 如何"实现接口 + 挂到链路"接入。

全部带 name（透明性红线）；全部无状态或只收集观测数据（不持有会话状态）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from runtime import DynamicValue, Result

from .types import ChainCtx, ChainEvent, NextFn, Verdict

__all__ = [
    "AddMetaPipe", "RedactPipe", "PrefixPipe", "RejectFilter", "AllowFilter",
    "RetryFilter", "FallbackFilter", "DegradeFilter", "AbortFilter",
    "RedactResultFilter", "TimeoutMiddleware", "PassMiddleware",
    "ShortCircuitMiddleware", "NeedsPortMiddleware", "TracingHook", "BoomHook",
]


class AddMetaPipe:
    """去程 pipe：给 payload 加一个键（示例：上下文规范化）。"""

    def __init__(self, name: str, key: str, value: DynamicValue) -> None:
        """记录组件名、要注入的键与值。"""
        self.name, self.key, self.value = name, key, value

    def transform(self, data: DynamicValue, ctx: ChainCtx) -> DynamicValue:
        """仅当 data 是字典时追加键，其它形态原样透传。"""
        if isinstance(data, dict):
            return {**data, self.key: self.value}
        return data


class RedactPipe:
    """去程 pipe：脱敏 payload 指定键（M8 PII 清洗的形态）。"""

    def __init__(self, name: str, key: str, replacement: str = "[REDACTED]") -> None:
        """记录组件名、要脱敏的键与替换文本。"""
        self.name, self.key, self.replacement = name, key, replacement

    def transform(self, data: DynamicValue, ctx: ChainCtx) -> DynamicValue:
        """字典中命中键则替换为脱敏文本，其它形态原样透传。"""
        if isinstance(data, dict) and self.key in data:
            return {**data, self.key: self.replacement}
        return data


class PrefixPipe:
    """回程 pipe：给字符串结果加前缀（示例大结果裁剪/变换）。"""

    def __init__(self, name: str, prefix: str = "«") -> None:
        """记录组件名与前缀文本。"""
        self.name, self.prefix = name, prefix

    def transform(self, data: DynamicValue, ctx: ChainCtx) -> DynamicValue:
        """字符串结果加前缀，其它形态原样透传。"""
        return f"{self.prefix}{data}" if isinstance(data, str) else data


class AllowFilter:
    """恒放行的 filter：用于占位 / 测试基线。"""

    def __init__(self, name: str = "allow-filter") -> None:
        """记录组件名。"""
        self.name = name

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """任何阶段都返回 allow。"""
        return Verdict.allow()


class RejectFilter:
    """去程护栏：命中关键词即 reject（首个 reject 生效，后续 filter 与 port 均不执行）。"""

    def __init__(self, name: str, keyword: str, *, on_response: bool = False) -> None:
        """记录组件名、命中关键词，以及匹配阶段（默认去程）。"""
        self.name, self.keyword, self.on_response = name, keyword, on_response

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """在声明的阶段里命中关键词则 reject，否则 allow。"""
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
        """记录组件名、重试次数与退避秒数。"""
        self.name, self.times, self.backoff = name, times, backoff

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """回程检测到 port 错误则返回 retry 判决。"""
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("retry", retry=self.times, backoff=self.backoff, reason="retry port error")
        return Verdict.allow()


class FallbackFilter:
    """回程兜底：port 失败时用预设值替换结果（skip 判决）。"""

    def __init__(self, name: str, fallback: DynamicValue) -> None:
        """记录组件名与兜底值。"""
        self.name, self.fallback = name, fallback

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """回程有错误则返回 skip 判决并携带兜底值。"""
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("skip", data=self.fallback, reason="fallback applied")
        return Verdict.allow()


class DegradeFilter:
    """回程降级：port 失败时切换到备用 port（degrade 判决）。"""

    def __init__(self, name: str, port: str) -> None:
        """记录组件名与目标 port 名。"""
        self.name, self.port = name, port

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """回程有错误则返回 degrade 判决，携带备用 port 名。"""
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("degrade", data=self.port, reason="switch to backup port")
        return Verdict.allow()


class AbortFilter:
    """回程中止：port 失败时终止整个 run（abort 判决）。"""

    def __init__(self, name: str) -> None:
        """记录组件名。"""
        self.name = name

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """回程有错误则返回 abort 判决。"""
        if phase == "response" and ctx.meta.get("error") is not None:
            return Verdict("abort", reason="abort on port error")
        return Verdict.allow()


class RedactResultFilter:
    """回程改写：用预设数据替换结果（redact 判决）。"""

    def __init__(self, name: str, data: DynamicValue) -> None:
        """记录组件名与替换数据。"""
        self.name, self.data = name, data

    def check(self, ctx: ChainCtx, phase: Literal["request", "response"]) -> Verdict:
        """回程返回 redact 判决并携带替换数据；去程恒放行。"""
        return Verdict("redact", data=self.data, reason="redacted by filter") if phase == "response" else Verdict.allow()


class TimeoutMiddleware:
    """超时中间件：为内层调用加 asyncio 超时（超时异常由洋葱层折叠为 Result）。"""

    def __init__(self, name: str, seconds: float) -> None:
        """记录组件名与超时秒数。"""
        self.name, self.seconds = name, seconds

    async def __call__(self, ctx: ChainCtx, nxt: NextFn) -> Result:
        """在超时窗口内执行下一层。"""
        return await asyncio.wait_for(nxt(), timeout=self.seconds)


class PassMiddleware:
    """调 next 但不返回：框架应透传内层结果。"""

    def __init__(self, name: str = "pass-mw") -> None:
        """记录组件名。"""
        self.name = name

    async def __call__(self, ctx: ChainCtx, nxt: NextFn) -> None:
        """仅推进内层，返回 None 让框架透传 box 中的结果。"""
        await nxt()


class ShortCircuitMiddleware:
    """未声明短路的红线样本：不调 next（需注册时 allow_short_circuit=True）。"""

    def __init__(self, name: str = "short-circuit-mw") -> None:
        """记录组件名。"""
        self.name = name

    async def __call__(self, ctx: ChainCtx, nxt: NextFn) -> Result:
        """直接返回结果，不调用 nxt（触发未声明短路检测）。"""
        return Result.success("short-circuited")


class NeedsPortMiddleware:
    """声明 port 依赖，供 freeze() 装配期检查。"""

    def __init__(self, name: str = "needs-llm", requires_ports: tuple[str, ...] = ("llm",)) -> None:
        """记录组件名与依赖的 port 名集合。"""
        self.name = name
        self.requires_ports = requires_ports

    async def __call__(self, ctx: ChainCtx, nxt: NextFn) -> Result:
        """透传内层结果。"""
        return await nxt()


class TracingHook:
    """只读观察：收集 ChainEvent（M9 TelemetryHook 的最小形态）。"""

    def __init__(self, name: str = "tracing-hook") -> None:
        """记录组件名并初始化事件收集列表。"""
        self.name = name
        self.events: list[ChainEvent] = []

    async def on_event(self, event: ChainEvent, ctx: ChainCtx) -> None:
        """把事件追加到收集列表。"""
        self.events.append(event)


class BoomHook:
    """故障注入：异常必须被吞掉且不影响主流程（M2-G2 隔离用例）。"""

    def __init__(self, name: str = "boom-hook") -> None:
        """记录组件名。"""
        self.name = name

    async def on_event(self, event: ChainEvent, ctx: ChainCtx) -> None:
        """总是抛错，用于验证观察者隔离。"""
        raise RuntimeError("observer failed")
