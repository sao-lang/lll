# purity-exempt-file: InMemoryEventStore 复用自 R1 为默认后端；events 为事件溯源动态边界(DynamicValue)
"""R10 持久化：CheckpointSnapshot + CheckpointManager（复用 R1 EventStore/replay）。

用户拍板：R10 不重复声明 EventStore（总设 §R10.4 的重复声明在文档同步时修正）。
checkpoint=短期 thread 内恢复（State.messages+scratch+last_*），memory=长期跨 thread
知识（R7）——两者分离，本层不碰 R7 记忆。

fingerprint = 事件序列的 SHA256（确定性、防漂移）；events 规范化为可 JSON 序列化的
dict 列表（与 R1 JsonSnapshot 对齐），回放经 R1 replay_events，不重造 fold。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from runtime import (
    AgentState,
    EventStore,
    InMemoryEventStore,
    JsonSnapshot,
    restore_checkpoint,
    replay_events,
)

__all__ = ["CheckpointSnapshot", "CheckpointManager", "replay_snapshot"]


@dataclass(frozen=True, slots=True)
class CheckpointSnapshot:
    """短期恢复快照：session_id/version/state/events/fingerprint。

    与 R1 JsonSnapshot 同构但为 R10 具名 DTO（观测语义，不是第二套存储协议）。
    """
    session_id: str
    version: str
    state: AgentState
    events: tuple[object, ...]
    fingerprint: str


def _fingerprint(events: tuple[object, ...]) -> str:
    """事件序列的确定性 SHA256（防篡改/漂移，墙钟不介入）。"""
    parts = []
    for event in events:
        effects = tuple(getattr(event, "effects", ()) or ())
        results = tuple(getattr(event, "results", ()) or ())
        parts.append(f"{[str(e) for e in effects]}|{[str(r) for r in results]}")
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:16]


def _to_json_snapshot(snap: CheckpointSnapshot) -> JsonSnapshot:
    """R10 DTO → R1 JsonSnapshot（可 JSON 持久化，结构兼容）。"""
    from runtime import StateSnapshot

    state = snap.state
    state_snapshot: StateSnapshot = {
        "messages": list(state.messages),
        "scratch": dict(state.scratch),
        "budgets": asdict(state.budgets),
        "version": state.version,
        "output": state.output,
        "last_skill": state.last_skill,
        "last_child": state.last_child,
        "last_wait_human": state.last_wait_human,
        "last_step_kinds": list(state.last_step_kinds),
        "last_tokens": state.last_tokens,
        "last_reason": state.last_reason,
    }
    return {
        "session_id": snap.session_id,
        "version": snap.version,
        "state": state_snapshot,
        "events": list(snap.events),
        "fingerprint": snap.fingerprint,
    }


class CheckpointManager:
    """checkpoint 存取：只依赖 R1 EventStore 协议，支持跨进程后端注入。"""
    name = "checkpoint_manager"

    def __init__(self, store: EventStore | None = None, version: str = "1.0") -> None:
        """构造管理器。

        Args:
            store: R1 EventStore 实现；None 用 InMemoryEventStore（默认后端）。
            version: 快照状态版本（迁移判据）。
        """
        self.store: EventStore = store if store is not None else InMemoryEventStore()
        self.version = version

    def snapshot(self, session_id: str, state: AgentState,
                 events: tuple[object, ...]) -> CheckpointSnapshot:
        """从 state + events 产具名快照（fingerprint 由事件确定性计算）。"""
        return CheckpointSnapshot(session_id=session_id, version=self.version,
                                  state=state, events=events,
                                  fingerprint=_fingerprint(events))

    async def save(self, snap: CheckpointSnapshot) -> None:
        """经 R1 EventStore 持久化（不自建存储协议）。"""
        await self.store.save(snap.session_id, _to_json_snapshot(snap))

    async def load(self, session_id: str) -> CheckpointSnapshot | None:
        """从 R1 EventStore 读回快照（跨进程恢复入口）。"""
        data = await self.store.load(session_id)
        if data is None:
            return None
        # R1 JsonSnapshot → R10 DTO：state 字段只取恢复回放所需的初始态；
        # events 原样保留供 replay_snapshot 回放（不依赖 JsonSnapshot.state 的最终态）。
        raw = data["state"]
        initial = AgentState()
        if isinstance(raw, dict):
            initial = initial.with_(scratch=dict(raw.get("scratch") or {}))
        events = tuple(data.get("events") or ())
        return CheckpointSnapshot(session_id=data["session_id"],
                                  version=data["version"], state=initial,
                                  events=events,
                                  fingerprint=data.get("fingerprint", ""))


def replay_snapshot(snap: CheckpointSnapshot) -> AgentState:
    """从快照的 events 回放终态（复用 R1 restore_checkpoint，不重造 fold）。

    load() 返回的 events 是 R1 JsonSnapshot 里的 plain dict。R1 的 revive() 只产
    Replayed(dict) 代理，isinstance(effect, Invoke/Terminate) 判断全失效，导致
    fold 静默丢状态（output=None、messages=()）。唯一正确的还原边界是 R1 的
    restore_checkpoint：它按 disposition 用 _effect_from_plain 重建真实 Effect、
    用 restore_state 还原状态，fold 的类型判断才成立。R1 零改动，本层只做 DTO→JsonSnapshot 组装。
    """
    # 回放起点用 load() 保留的初始态（scratch）；终态由 events 重建。
    # state 段复用 _to_json_snapshot 的规范化口径（AgentState → R1 StateSnapshot）。
    from runtime import Program

    def _empty_program() -> Program:  # pragma: no cover - 回放不驱动 Program
        return (lambda: None)  # type: ignore[return-value]

    jsonsnap: JsonSnapshot = _to_json_snapshot(snap)
    restored = restore_checkpoint(jsonsnap, _empty_program)
    # restore_checkpoint 只重建 Event（含真实 Effect）+ 初始态；终态需 R1 replay fold。
    return replay_events(restored.events, restored.state)
