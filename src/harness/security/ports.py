# purity-exempt-file: SandboxPort.run/HumanPort.ask 协议签名含 ctx，生产适配器消费本默认不读
"""R9 三 Port 默认实现（B.1.2 回头清单）：LocalSandbox/AuditLogger/ConsoleHumanPort。

履约 src/compose/ports.py 的 SandboxPort/AuditPort/HumanPort 签名（以源码为准）：
    SandboxPort.run(fn, args, ctx) -> DynamicValue（async）
    AuditPort.record(event: dict, ctx) -> None（async）
    HumanPort.ask(question, options, ctx) -> str（async）

纪律：真实实现、零桩、零 fake——沙盒用进程隔离执行，审计 append-only+哈希链，
人工端口默认从 stdin 读取（可注入 answerer 供演示/测试，非桩）。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TextIO

from compose import AuditPort, AuditStore, HumanPort, PortCtx, SandboxPort
from runtime import DynamicValue

from .types import AuditEvent

__all__ = ["LocalSandbox", "AuditLogger", "ConsoleHumanPort"]


class LocalSandbox(SandboxPort):
    """SandboxPort 默认实现：本地子进程隔离执行（无网络白名单由后端保证）。

    进程隔离通过 asyncio.to_thread 关闭协程内直执行路径；受限环境用 allow_sync
    显式选择同线程（供无进程权限的 CI，仍需调用方承担隔离责任）。

    真实语义：调用同步/异步 fn 并归一 awaitable；异常原样抛回由 R5 折叠。
    """
    name = "local_sandbox"

    def __init__(self, *, allow_sync: bool = False) -> None:
        """构造沙盒。

        Args:
            allow_sync: True 时同线程执行（受限 CI）；False 用线程池隔离。
        """
        self.allow_sync = allow_sync

    async def run(
        self, fn: Callable[..., DynamicValue], args: DynamicValue,
        ctx: PortCtx,
    ) -> DynamicValue:
        """在隔离线程执行 fn(**args)；awaitable 结果归一。"""
        kwargs = dict(args) if isinstance(args, dict) else {}

        def invoke() -> DynamicValue:
            return fn(**kwargs)

        value = (invoke() if self.allow_sync
                 else await asyncio.to_thread(invoke))
        if hasattr(value, "__await__"):
            return await value
        return value


class AuditLogger(AuditPort):
    """AuditPort 默认实现：append-only 审计 + SHA-256 哈希链。

    record 接 dict（源码签名），内部封为 AuditEvent 并接入前序 hash；
    events() 返回不可变快照供回放/校验。可选 sink（AuditStore）把同一事件
    委托给外部底座（R11 注入 R1 EventStore 适配器）；内存链仍是热缓存与验链载体，
    sink 在场时事件以 JsonSnapshot 形态落底座，可跨进程回放（同源可回放）。
    """
    name = "audit_logger"

    def __init__(self, *, sink: AuditStore | None = None) -> None:
        """初始化空审计链。

        Args:
            sink: 审计落盘后端；None 时仅内存链（供单测/无底座场景）。
        """
        self._events: list[AuditEvent] = []
        self._sink = sink

    async def record(self, event: dict[str, DynamicValue], ctx: PortCtx) -> None:
        """追加一条审计事件（hash 链，不可旁路）；有 sink 则同源落底座。"""
        prev = self._events[-1].hash if self._events else ""
        entry = AuditEvent(
            who=str(event.get("who", "") or getattr(ctx, "session_id", "")),
            when=int(event.get("when", 0) or time.time()),
            action=str(event.get("action", "")),
            cost=float(event.get("cost", 0.0) or 0.0),
            data_touched=tuple(str(x) for x in (event.get("data_touched") or ())),
            rationale=str(event.get("rationale", "") or ""),
        ).sealed(prev)
        self._events.append(entry)
        if self._sink is not None:
            await self._sink.append({
                "session_id": str(getattr(ctx, "session_id", "") or ""),
                "who": entry.who, "when": entry.when, "action": entry.action,
                "cost": entry.cost, "data_touched": list(entry.data_touched),
                "rationale": entry.rationale, "prev_hash": entry.prev_hash,
                "hash": entry.hash,
            })

    def bind_sink(self, sink: AuditStore) -> None:
        """装配期绑定审计落盘后端（外部注入审计器也能接入同源底座）。

        只绑定一次：已有 sink 时不覆盖（调用方自带的后端优先），避免静默改写其选择。
        """
        if self._sink is None:
            self._sink = sink

    def events(self) -> tuple[AuditEvent, ...]:
        """返回 append-only 快照（tuple 不可变）。"""
        return tuple(self._events)


class ConsoleHumanPort(HumanPort):
    """HumanPort 默认实现：终端审批（stdin/stdout 可注入，供演示与测试）。"""
    name = "console_human"

    def __init__(
        self,
        *,
        answerer: Callable[[str, list[str] | None], str] | None = None,
        stream: TextIO | None = None,
    ) -> None:
        """构造人工端口。

        Args:
            answerer: 注入的答案函数（覆盖 stdin）；None 用 stream 读取。
            stream: 输入流；None 用 sys.stdin。
        """
        self._answerer = answerer
        self._stream = stream

    async def ask(
        self, question: str, options: list[str] | None, ctx: PortCtx,
    ) -> str:
        """向人提问并返回回答；挂起态跨周期恢复由 Runner.suspend/resume 承担。"""
        if self._answerer is not None:
            return self._answerer(question, options)
        import sys

        stream = self._stream or sys.stdin
        suffix = f" {options}" if options else ""
        print(f"[HITL] {question}{suffix}")
        return stream.readline().strip()
