"""R9-T05 审计：append-only + 哈希链可验证，覆盖全部事件。"""
from __future__ import annotations

import asyncio

from compose import PortCtx
from harness.security import AuditLogger, verify_hash_chain


def _ctx() -> PortCtx:
    return PortCtx(session_id="s9", run_id="r9")


def _event(when: int, action: str, cost: float = 0.0) -> dict:
    """按 AuditPort 源码签名（dict 载荷）构造审计事件。"""
    return {"who": "agent", "when": when, "action": action, "cost": cost,
            "data_touched": ("f1",), "rationale": "test"}


def test_t05_audit_hash_chain() -> None:
    """审计哈希链：每条 prev_hash 接上一条 hash，首条 prev_hash 为空。"""
    logger = AuditLogger()
    asyncio.run(logger.record(_event(1, "tool.call"), _ctx()))
    asyncio.run(logger.record(_event(2, "tool.call", 0.1), _ctx()))
    records = logger.events()
    assert len(records) == 2
    assert records[0].prev_hash == ""
    assert records[1].prev_hash == records[0].hash
    assert verify_hash_chain(records)


def test_t05_tamper_detected() -> None:
    """篡改任一条后哈希链校验失败（防篡改）。"""
    logger = AuditLogger()
    asyncio.run(logger.record(_event(1, "tool.call"), _ctx()))
    original = logger.events()[0]
    from harness.security import AuditEvent
    tampered = (AuditEvent(who="hacker", when=original.when, action=original.action,
                           cost=original.cost, data_touched=original.data_touched,
                           rationale=original.rationale, prev_hash=original.prev_hash,
                           hash=original.hash),)
    assert not verify_hash_chain(tampered)


def test_t05_append_only() -> None:
    """append-only：events() 返回不可变快照，外部改不动内部。"""
    logger = AuditLogger()
    asyncio.run(logger.record(_event(1, "x"), _ctx()))
    snap = logger.events()
    assert isinstance(snap, tuple)
    assert len(snap) == 1
