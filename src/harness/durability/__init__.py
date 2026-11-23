"""R10 持久化（durability）：checkpoint 快照/回放，复用 R1 EventStore。

checkpoint=短期 thread 内恢复；memory=长期跨 thread 知识（R7）——分离不混用。
"""
from __future__ import annotations

from .checkpoint import CheckpointManager, CheckpointSnapshot, replay_snapshot

__all__ = ["CheckpointManager", "CheckpointSnapshot", "replay_snapshot"]
