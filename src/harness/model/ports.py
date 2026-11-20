"""R4 Port 扩展协议（规格 P4）：LLMPortWithUsage 承载 last_usage。

last_usage 从 R2 基础 Port（hooks.LLMPort）归位到 R4 能力层扩展协议：
基础 Port 只声明 complete/stream 的通用能力，流式 usage 结算属 R4 领域语义，
不该反向污染 R2。真实后端（OpenAI stream_options.include_usage）在末块写入
last_usage，网关读它结算成本——不估算、不兜零。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from compose import LLMPort, Usage

__all__ = ["LLMPortWithUsage"]


@runtime_checkable
class LLMPortWithUsage(LLMPort, Protocol):
    """R4 扩展：实现 LLMPort 且流式路径写 last_usage 的后端。"""

    last_usage: Usage | None
