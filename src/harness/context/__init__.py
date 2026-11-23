"""R7 上下文工程：分段组装、阈值压缩、重注入、大结果卸载与显式编排。

公共 API 见 docs/02-模块详设/Agent/R7-上下文与记忆.md。
"""
from __future__ import annotations

from .assembler import ContextAssembler, DefaultContextAssembler, SYSTEM_SECTION_NAME
from .capability import MemoryCapability
from .compaction import CompactionPolicy, DefaultCompactionPolicy, count_tokens
from .manager import ContextMemoryManager
from .observability import CacheTelemetry
from .offloader import (
    BLOB_HANDLE_SCHEME,
    BlobPort,
    DefaultOffloader,
    InMemoryBlobStore,  # purity-exempt: 生产内存 blob 后端（存量，行号位移），非测试替身
    Offloader,
    make_handle,
    parse_handle,
)
from .reinjector import DefaultReinjector, Reinjector
from .token_counter import DefaultTokenCounter, TokenCounter, estimate_tokens
# 记忆 DTO 重导出：供 ContextMemoryManager 消费者与门禁符号检查可见（源仍在 harness.memory）。
from harness.memory import MemoryScope, ScopeResolver, make_item_id
from .types import (
    DEFAULT_KEEP_RECENT,
    DEFAULT_PIN_PATTERNS,
    DEFAULT_TRIGGER_TURNS,
    TRIGGER_RATIO,
    ContextSection,
    TrimPolicy,
)

__all__ = [
    "ContextSection",
    "TrimPolicy",
    "TRIGGER_RATIO",
    "DEFAULT_TRIGGER_TURNS",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_PIN_PATTERNS",
    "ContextAssembler",
    "DefaultContextAssembler",
    "SYSTEM_SECTION_NAME",
    "CompactionPolicy",
    "DefaultCompactionPolicy",
    "count_tokens",
    "Reinjector",
    "DefaultReinjector",
    "TokenCounter",
    "DefaultTokenCounter",
    "estimate_tokens",
    "MemoryScope",
    "ScopeResolver",
    "make_item_id",
    "ContextMemoryManager", "MemoryCapability",
    "CacheTelemetry",
    "BlobPort",
    "Offloader",
    "InMemoryBlobStore",  # purity-exempt: 生产默认后端导出名（存量，行号位移）
    "DefaultOffloader",
    "BLOB_HANDLE_SCHEME",
    "make_handle",
    "parse_handle",
]
