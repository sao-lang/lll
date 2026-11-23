# purity-exempt-file: 接线层：HumanPort/AuditPort 协议包装、DynamicValue 边界、object 为协议形状
"""R11 §11 六项跨模块真实接线（全部落在装配层，不改 R1/R5/R9 源码）。

依赖只向下：api(L3) → compose/harness(L1/L2) → runtime(L0)；工具层互不直连，
跨模块装配只在此发生。每项接线都有失败测试（tests/r11/test_wiring.py）。

六项：①审批流 ②审计哈希链 ③secret 脱敏 ④事件流投影 ⑤checkpoint ⑥CI 评测 gate。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence

from compose import AuditStore, DangerLevel, LifecycleBus, LifecyclePhase, Notice, PortCtx
from harness.durability import CheckpointManager, CheckpointSnapshot
from harness.observability import EventEnvelope, InMemoryEventStream
from harness.security import (
    ApprovalFlow,
    AuditEvent,
    AuditLogger,
    RuleGuardrail,
    SafetyVerdict,
    verify_hash_chain,
)
from runtime import DynamicValue, EventStore, JsonSnapshot


STREAM_TOPIC = "trace"


class ApprovingHumanPort:
    """① 审批流接线：包装 HumanPort，consult ApprovalFlow；超时→deny。

    不改 R5：ToolRegistry 构造期 `human=` 注入本包装器即可（CRITICAL 分支调 ask）。
    danger 由调用方经 ask 的可选关键字注入（R5 不写 ctx.meta，这里显式传参）。
    """
    name = "approving_human"

    def __init__(self, inner: object, flow: ApprovalFlow | None = None) -> None:
        """构造审批包装器。

        Args:
            inner: 被包装的 HumanPort（ConsoleHumanPort 或自定义）。
            flow: 审批流；None 用默认（CRITICAL 触发，timeout=300）。
        """
        self._inner = inner
        self._flow = flow or ApprovalFlow()

    async def ask(self, question: str, options: list[str] | None, ctx: PortCtx,
                  *, danger: DangerLevel | None = None) -> str:
        """先 should_approve，再限时等待；任何超时/异常一律 deny（fail-closed）。

        danger 显式入参优先；未传时读 ctx.meta["danger"]（R5 ToolRegistry 路由写入），
        两者都无才回退 CRITICAL（最严格默认，fail-closed）。
        """
        if danger is not None:
            level = danger
        else:
            from_meta = ctx.meta.get("danger")
            level = DangerLevel.coerce(from_meta) if from_meta is not None else DangerLevel.CRITICAL
        if not self._flow.should_approve(level):
            return self._flow.decide_timeout()
        try:
            answer = await asyncio.wait_for(
                self._inner.ask(question, options, ctx),  # type: ignore[attr-defined]
                timeout=max(0, self._flow.timeout),
            )
        except (TimeoutError, asyncio.TimeoutError):
            return self._flow.decide_timeout()
        return str(answer)

_AUDIT_KEY_PREFIX = "__audit__:"


class R1AuditEventSink(AuditStore):
    """审计同源底座：把审计事件以 R1 JsonSnapshot 形态写入共享 R1 EventStore。

    依赖只向下（api → runtime 协议）；审计链哈希由 AuditLogger 生成，本 sink 只做
    append-only 持久化（读旧快照→追加→写回），不新造事件日志底座。replay 从同一
    store 读回并重建 AuditEvent 序列 → 同源可回放（兑现 R9 §11 承诺）。
    """
    name = "r1_audit_event_sink"

    def __init__(self, store: EventStore) -> None:
        """构造审计 sink。

        Args:
            store: 与 Runner(checkpoint) 共享的 R1 EventStore 实例。
        """
        self.store = store

    def key_of(self, session_id: str) -> str:
        """审计保留 session 键（与业务 session 隔离，避免污染事件日志）。"""
        return f"{_AUDIT_KEY_PREFIX}{session_id}"

    async def append(self, event: dict[str, DynamicValue]) -> None:
        """追加一条审计事件到该 session 的 R1 快照（append-only）。"""
        sid = str(event.get("session_id", "") or "")
        key = self.key_of(sid)
        snap = await self.store.load(key)
        events = list(snap.get("events") or ()) if snap is not None else []
        events.append(dict(event))
        await self.store.save(key, self._snapshot(key, events))

    @staticmethod
    def _snapshot(key: str, events: list[DynamicValue]) -> JsonSnapshot:
        """构造 R1 JsonSnapshot（state 为空、events 为审计事件、fingerprint 稳定）。"""
        import hashlib
        from typing import cast

        from runtime import AgentState, StateSnapshot, canonical
        from runtime.types import _plain

        return cast(JsonSnapshot, {
            "session_id": key,
            "version": "audit-1.0",
            "state": cast(StateSnapshot, _plain(AgentState())),
            "events": events,
            "fingerprint": hashlib.sha256(canonical(events).encode("utf-8")).hexdigest(),
        })


async def replay_audit_events(sink: R1AuditEventSink, session_id: str) -> tuple[AuditEvent, ...]:
    """从 R1 EventStore 回放审计事件，重建 AuditEvent 序列（同源可回放）。"""
    snap = await sink.store.load(sink.key_of(session_id))
    if snap is None:
        return ()
    rebuilt: list[AuditEvent] = []
    for raw in snap.get("events") or ():
        if not isinstance(raw, dict):
            continue
        data_touched = raw.get("data_touched") or ()
        rebuilt.append(AuditEvent(
            who=str(raw.get("who", "") or ""),
            when=int(raw.get("when", 0) or 0),
            action=str(raw.get("action", "") or ""),
            cost=float(raw.get("cost", 0.0) or 0.0),
            data_touched=tuple(str(x) for x in data_touched),
            rationale=str(raw.get("rationale", "") or ""),
            prev_hash=str(raw.get("prev_hash", "") or ""),
            hash=str(raw.get("hash", "") or ""),
        ))
    return tuple(rebuilt)


class VerifyingAuditPort:
    """② 审计哈希链接线：包装 AuditLogger，每次 record 后验链。

    不改 R9：构造期 `audit=` 注入。验链失败抛 ValueError，绝不静默。
    """
    name = "verifying_audit"

    def __init__(self, inner: AuditLogger | None = None) -> None:
        """构造审计包装器（默认包 AuditLogger）。"""
        self._inner = inner or AuditLogger()

    async def record(self, event: dict[str, DynamicValue], ctx: PortCtx) -> None:
        """追加事件后立刻验链（链断即抛，不进后续步骤）。"""
        await self._inner.record(event, ctx)
        self.verify_external(self._inner.events())

    def events(self) -> tuple[object, ...]:
        """不可变事件快照（供回放/核对）。"""
        return self._inner.events()

    def verify_external(self, events: Sequence[object]) -> None:
        """校验给定事件序列；断链 ValueError（供外部注入篡改检测）。"""
        if not verify_hash_chain(events):  # type: ignore[arg-type]
            raise ValueError("audit hash chain broken")


def verify_audit_chain(port: VerifyingAuditPort) -> bool:
    """公开校验 API：对当前审计链验签，True/False（不抛）。"""
    try:
        port.verify_external(port.events())
        return True
    except ValueError:
        return False


def redact_output(value: object, ctx: PortCtx, *, secret_refs: Sequence[str],
                  source: str = "tool", direction: str = "output") -> SafetyVerdict:
    """③ secret 脱敏接线：构造 SecurityCtx 注入 secret_refs，过护栏得 redact 裁决。

    不改 R9：SecurityCtx.from_port_ctx 复制 meta，护栏从 meta['secret_refs'] 读取。
    返回 SafetyVerdict（action='redact' 时 redacted 为脱敏文本）。
    """
    from harness.security import SecurityCtx

    sec_ctx = SecurityCtx.from_port_ctx(ctx, source=source, direction=direction)
    sec_ctx.meta["secret_refs"] = [str(s) for s in secret_refs if s]
    return RuleGuardrail().check(value, sec_ctx)


def bind_stream_projection(
    stream: InMemoryEventStream | None = None,
    *,
    clock: Callable[[], float] | None = None,
) -> LifecycleBus:
    """④ 事件流投影接线：LifecycleBus 订阅三相位 → publish EventEnvelope。

    ts 来自注入 clock（真实墙钟，注入假时钟可精确断言；绝不造假 float(n-1)）；
    seq 优先取 Notice.seq（R1 Event.seq 的只读投影，由通知源携带），缺席时回退
    payload["seq"]，再退到本进程单调源（显式标注合成，不再冒充 R1 序号）。
    返回 bus 供装配期同时注入 Runner(notify=runner_notifier(bus)) 与
    ModelGateway(bus=bus)；after_model 不经 Runner，必须经网关。
    """
    target = stream or InMemoryEventStream()
    bus = LifecycleBus()
    wall_clock = clock or time.time
    fallback = 0
    watched = (LifecyclePhase.AFTER_MODEL, LifecyclePhase.AFTER_TOOL,
               LifecyclePhase.SESSION_END)

    def publish_notice(notice: Notice) -> None:
        """把只读 Notice 投影为 EventEnvelope 发布到单一 topic。"""
        nonlocal fallback
        seq = notice.seq
        if seq <= 0:
            seq = int(notice.payload.get("seq", 0) or 0)  # 通知源未填 DTO 字段时兼容 payload 投影
        if seq <= 0:
            fallback += 1
            seq = fallback
        target.publish(EventEnvelope(
            seq=seq, topic=STREAM_TOPIC, ts=wall_clock(),
            type=notice.phase.value,
            data={"session_id": notice.session_id, "port": notice.port,
                  **dict(notice.payload)},
        ))

    for phase in watched:
        bus.on(phase, publish_notice, name="r10-stream-projection")
    return bus


class CheckpointEventStore:
    """⑤ checkpoint 接线：R1 EventStore 适配器 → R10 CheckpointManager。

    Runner(store=...) 在挂起/每 N 步/终态调 save；本适配器转调 mgr.save(snapshot)，
    load 原样返回 JsonSnapshot（R1 恢复路径需要 JSON 形态）。终态回放用
    CheckpointManager.replay_snapshot（内存 DTO，避免 load() 的 dict 未 revive）。
    """
    name = "checkpoint_event_store"

    def __init__(self, manager: CheckpointManager | None = None,
                 inner: EventStore | None = None) -> None:
        """构造适配器（消除双写：manager 与 inner 共享同一 R1 EventStore）。

        Args:
            manager: R10 CheckpointManager（缺省基于注入 inner 构造）。
            inner: R1 EventStore 实现（缺省 InMemoryEventStore）；生产可注入 DB/Redis。
        """
        from runtime import InMemoryEventStore

        self.inner = inner if inner is not None else InMemoryEventStore()
        self._mgr = manager or CheckpointManager(store=self.inner)

    async def save(self, session_id: str, snapshot: JsonSnapshot) -> None:
        """保存：真委托内层 EventStore（R1 形态）+ mgr.save（R10 形态）。"""
        from runtime import AgentState
        from runtime.types import restore_state

        await self.inner.save(session_id, snapshot)
        state = restore_state(snapshot.get("state")) if "state" in snapshot else AgentState()
        dto = self._mgr.snapshot(session_id, state, tuple(snapshot.get("events") or ()))
        await self._mgr.save(replace_dto_fingerprint(dto, str(snapshot.get("fingerprint", ""))))

    async def load(self, session_id: str) -> JsonSnapshot | None:
        """加载：真委托共享的 R1 EventStore（非本地 dict 桩）。"""
        return await self.inner.load(session_id)

    def manager(self) -> CheckpointManager:
        """返回被消费的 CheckpointManager（供装配期核对/回放）。"""
        return self._mgr


def replace_dto_fingerprint(dto: CheckpointSnapshot, fingerprint: str) -> CheckpointSnapshot:
    """用 R1 确定性指纹替换 R10 快照指纹（同事件序列口径一致）。"""
    from dataclasses import replace

    return replace(dto, fingerprint=fingerprint)
