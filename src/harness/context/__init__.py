"""R7 上下文工程：分段组装、阈值压缩、大结果卸载与显式编排。

公共 API 见 docs/02-模块详设/Agent/R7-上下文与记忆.md。
"""
from __future__ import annotations

from .assembler import ContextAssembler, DefaultContextAssembler, SYSTEM_SECTION_NAME
from .compaction import CompactionPolicy, DefaultCompactionPolicy, count_tokens
from .manager import ContextMemoryManager
from .offloader import (
    BLOB_HANDLE_SCHEME,
    BlobPort,
    DefaultOffloader,
    InMemoryBlobStore,
    Offloader,
    make_handle,
    parse_handle,
)
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
    "ContextMemoryManager",
    "BlobPort",
    "Offloader",
    "InMemoryBlobStore",
    "DefaultOffloader",
    "BLOB_HANDLE_SCHEME",
    "make_handle",
    "parse_handle",
]
