"""R9 安全域 DTO（R9 §4）：SecurityCtx/SafetyVerdict/SafetyReport/AuditEvent。

全部 frozen+slots；安全四态用 SafetyVerdict（区别于已删除的组合层通用 Verdict）；
SecurityCtx 是 Invocation/Notice 面向安全域的具名投影，检测器不直接依赖全量上下文。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from compose import DangerLevel, Invocation, PortCtx
from harness.skills import InjectionVerdict
from runtime import DynamicValue

__all__ = [
    "SecurityCtx", "SafetyVerdict", "SafetyReport", "AuditEvent",
]

type _Source = Literal["user", "tool", "rag", "memory", "skill", "web"]
type _Direction = Literal["input", "output"]
_VALID_SOURCES: tuple[str, ...] = ("user", "tool", "rag", "memory", "skill", "web")


def _coerce_source(value: str) -> _Source:
    """把外来 source 归一为合法 Literal（非法回退 tool）。"""
    return cast(_Source, value if value in _VALID_SOURCES else "tool")


def _coerce_direction(value: str) -> _Direction:
    """把外来 direction 归一为合法 Literal（非法回退 input）。"""
    return cast(_Direction, value if value in ("input", "output") else "input")


@dataclass(frozen=True, slots=True)
class SecurityCtx:
    """安全检查上下文：Invocation/PortCtx 面向安全域的具名投影。

    字段：
        session_id/run_id: 审计关联标识。
        source: 内容来源（user/tool/rag/memory/skill/web）——按来源分策。
        direction: input/output 护栏方向。
        danger: 当前操作危险级别。
        trust: 内容是否来自可信内核（False 必过注入检测）。
        meta: 扩展元数据（规则 3 动态边界）。
    """
    session_id: str
    run_id: str
    source: Literal["user", "tool", "rag", "memory", "skill", "web"]
    direction: Literal["input", "output"]
    danger: DangerLevel = DangerLevel.SAFE
    trust: bool = False
    meta: dict[str, DynamicValue] = field(default_factory=dict)

    @classmethod
    def from_invocation(
        cls, inv: Invocation, *, source: str = "tool",
        direction: str = "input", trust: bool = False,
    ) -> SecurityCtx:
        """从 Invocation 投影（不改原对象，避免安全域依赖全量组合层字段语义）。"""
        meta = getattr(inv, "meta", None)
        raw_danger = (meta or {}).get("danger") if isinstance(meta, dict) else None
        try:
            danger = DangerLevel.coerce(raw_danger)
        except (TypeError, ValueError):
            danger = DangerLevel.CRITICAL  # fail closed
        return cls(
            session_id=str(getattr(inv, "session_id", "") or ""),
            run_id=str(getattr(inv, "run_id", "") or ""),
            source=_coerce_source(source),
            direction=_coerce_direction(direction),
            danger=danger,
            trust=trust,
            meta=dict(meta) if isinstance(meta, dict) else {},
        )

    @classmethod
    def from_port_ctx(
        cls, ctx: PortCtx, *, source: str = "tool",
        direction: str = "input", trust: bool = False,
    ) -> SecurityCtx:
        """从 PortCtx 投影（R9 三 Port 的消费侧便捷入口）。"""
        return cls(
            session_id=ctx.session_id, run_id=ctx.run_id,
            source=_coerce_source(source),
            direction=_coerce_direction(direction),
            trust=trust,
            meta=dict(ctx.meta),
        )


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """安全域护栏判定：allow/redact/deny/ask 四态。

    ask 不直接拦截，由框架产 Suspend(question, options) + wait_human/resumed 通知；
    redact 经 redacted 返回脱敏内容，不复用组合层通用裁决。
    """
    action: Literal["allow", "redact", "deny", "ask"]
    redacted: DynamicValue = None
    reason: str = ""
    rule: str = ""


@dataclass(frozen=True, slots=True)
class SafetyReport:
    """注入检测结果：safe/suspicious/malicious 三级（从 InjectionVerdict 派生）。"""
    level: Literal["safe", "suspicious", "malicious"]
    rules_hit: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence: str = ""

    @staticmethod
    def from_verdict(verdict: InjectionVerdict, *, rule: str = "",
                     evidence: str = "") -> SafetyReport:
        """单一检测器裁决 → 安全域报告（复用 skills 检测器，不造第二套）。"""
        if verdict is InjectionVerdict.INJECTED:
            return SafetyReport(level="malicious", rules_hit=(rule,) if rule else (),
                                confidence=1.0, evidence=evidence)
        return SafetyReport(level="safe", confidence=0.0)


def _event_hash(who: str, when: int, action: str, cost: float,
                data_touched: tuple[str, ...], rationale: str, prev_hash: str) -> str:
    """审计条目哈希（纯函数、确定性，SHA-256 截断前 16 位便于观察）。"""
    import hashlib

    payload = f"{who}|{when}|{action}|{cost}|{','.join(data_touched)}|{rationale}|{prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """审计条目（append-only + 哈希链），hash 由字段确定性计算，不由调用方伪造。"""
    who: str
    when: int
    action: str
    cost: float
    data_touched: tuple[str, ...]
    rationale: str
    prev_hash: str = ""
    hash: str = ""

    def sealed(self, prev_hash: str) -> AuditEvent:
        """返回接上前序 hash 的封印条目（不可变原对象保持不变）。"""
        digest = _event_hash(self.who, self.when, self.action, self.cost,
                             self.data_touched, self.rationale, prev_hash)
        return AuditEvent(
            who=self.who, when=self.when, action=self.action, cost=self.cost,
            data_touched=self.data_touched, rationale=self.rationale,
            prev_hash=prev_hash, hash=digest,
        )


def verify_hash_chain(events: tuple[AuditEvent, ...]) -> bool:
    """校验哈希链：首条 prev_hash 为空，每条 hash 与字段重算一致且接上一条。"""
    prev = ""
    for event in events:
        if event.prev_hash != prev:
            return False
        expected = _event_hash(event.who, event.when, event.action, event.cost,
                               event.data_touched, event.rationale, event.prev_hash)
        if event.hash != expected:
            return False
        prev = event.hash
    return True
