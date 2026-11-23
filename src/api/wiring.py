# purity-exempt-file: 接线层：HumanPort/AuditPort 协议包装、DynamicValue 边界、object 为协议形状
"""R11 §11 六项跨模块真实接线（全部落在装配层，不改 R1/R5/R9 源码）。

依赖只向下：api(L3) → compose/harness(L1/L2) → runtime(L0)；工具层互不直连，
跨模块装配只在此发生。每项接线都有失败测试（tests/r11/test_wiring.py）。

六项：①审批流 ②审计哈希链 ③secret 脱敏 ④事件流投影 ⑤checkpoint ⑥CI 评测 gate。
"""
from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence

from compose import DangerLevel, LifecycleBus, LifecyclePhase, Notice, PortCtx
from harness.durability import CheckpointManager, CheckpointSnapshot
from harness.observability import EventEnvelope, InMemoryEventStream
from harness.security import ApprovalFlow, AuditLogger, RuleGuardrail, SafetyVerdict, verify_hash_chain
from runtime import DynamicValue, JsonSnapshot


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
        """先 should_approve，再限时等待；任何超时/异常一律 deny（fail-closed）。"""
        level = danger if danger is not None else DangerLevel.CRITICAL
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


def bind_stream_projection(stream: InMemoryEventStream | None = None) -> LifecycleBus:
    """④ 事件流投影接线：LifecycleBus 订阅三相位 → publish EventEnvelope。

    同步 NoticeFn：相位→topic 映射，seq 由重启计数提供（Notice 无 seq/ts）。
    返回 bus 供装配期同时注入 Runner(notify=runner_notifier(bus)) 与
    ModelGateway(bus=bus)；after_model 不经 Runner，必须经网关。
    """
    target = stream or InMemoryEventStream()
    bus = LifecycleBus()
    seq = itertools.count(1)
    watched = (LifecyclePhase.AFTER_MODEL, LifecyclePhase.AFTER_TOOL,
               LifecyclePhase.SESSION_END)

    def publish_notice(notice: Notice) -> None:
        """把只读 Notice 投影为 EventEnvelope 发布到 <phase> topic。"""
        # 单 topic（EventStream 精确匹配无通配），相位放 type 字段可续读。
        n = next(seq)
        target.publish(EventEnvelope(
            seq=n, topic=STREAM_TOPIC, ts=float(n - 1),
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
                 inner: object | None = None) -> None:
        """构造适配器。

        Args:
            manager: R10 CheckpointManager（缺省用 R1 EventStore 默认后端）。
            inner: R1 EventStore 实现（缺省 InMemoryEventStore）；生产可注入 DB/Redis。
        """
        from runtime import InMemoryEventStore

        self._mgr = manager or CheckpointManager()
        self._inner = inner if inner is not None else InMemoryEventStore()

    async def save(self, session_id: str, snapshot: JsonSnapshot) -> None:
        """保存：真委托内层 EventStore（R1 形态）+ mgr.save（R10 形态）。"""
        from runtime import AgentState
        from runtime.types import restore_state

        await self._inner.save(session_id, snapshot)  # type: ignore[attr-defined]
        state = restore_state(snapshot.get("state")) if "state" in snapshot else AgentState()
        dto = self._mgr.snapshot(session_id, state, tuple(snapshot.get("events") or ()))
        await self._mgr.save(replace_dto_fingerprint(dto, str(snapshot.get("fingerprint", ""))))

    async def load(self, session_id: str) -> JsonSnapshot | None:
        """加载：真委托内层 EventStore（非本地 dict 桩）。"""
        return await self._inner.load(session_id)  # type: ignore[attr-defined,no-any-return]

    def manager(self) -> CheckpointManager:
        """返回被消费的 CheckpointManager（供装配期核对/回放）。"""
        return self._mgr


def replace_dto_fingerprint(dto: CheckpointSnapshot, fingerprint: str) -> CheckpointSnapshot:
    """用 R1 确定性指纹替换 R10 快照指纹（同事件序列口径一致）。"""
    from dataclasses import replace

    return replace(dto, fingerprint=fingerprint)
