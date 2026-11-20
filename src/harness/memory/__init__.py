"""R7 长期记忆：MemoryPort / StorePort 默认实现与双轴分类 DTO。

公共 API 见 docs/02-模块详设/Agent/R7-上下文与记忆.md。
"""
from __future__ import annotations

from .memory import MemoryStore
from .store import InMemoryStorePort
from .types import (
    MMR_LAMBDA,
    NEAR_DUP_SIM_THRESHOLD,
    PREFERENCE_REPEAT_THRESHOLD,
    PROFILE_CONFIDENCE_THRESHOLD,
    RERANK_W_CONFIDENCE,
    RERANK_W_IMPORTANCE,
    RERANK_W_RECENCY,
    RERANK_W_RELEVANCE,
    SCOPE_META_KEY,
    SECRET_KEY_TOKENS,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryTier,
    ScopeResolver,
    is_secret_key,
    make_item_id,
)

__all__ = [
    "MemoryItem",
    "MemoryKind",
    "MemoryQuery",
    "MemoryScope",
    "MemoryTier",
    "ScopeResolver",
    "SECRET_KEY_TOKENS",
    "SCOPE_META_KEY",
    "NEAR_DUP_SIM_THRESHOLD",
    "PREFERENCE_REPEAT_THRESHOLD",
    "PROFILE_CONFIDENCE_THRESHOLD",
    "RERANK_W_RELEVANCE",
    "RERANK_W_IMPORTANCE",
    "RERANK_W_RECENCY",
    "RERANK_W_CONFIDENCE",
    "MMR_LAMBDA",
    "is_secret_key",
    "make_item_id",
    "MemoryStore",
    "InMemoryStorePort",
]
