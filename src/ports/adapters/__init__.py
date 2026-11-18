"""具体端口适配器实现：外部系统的真实 IO 后端（不联网由注入 fake client 保证）。

已落地：
- llm.py  OpenAICompatLLM（R4 LLMPort 默认实现）
- mcp.py  StdioMCPTransport / HttpMCPTransport（R5 MCPTransport 默认实现，stdio/http）
- webfetch.py  HttpFetcher（R5 WebFetcher 默认实现，真 GET + 注入检测联动）
"""
from __future__ import annotations

from .llm import OpenAICompatLLM
from .mcp import (
    HttpMCPTransport,
    StdioChannel,
    StdioMCPTransport,
    SubprocessStdioChannel,
)
from .webfetch import HttpFetcher

__all__ = [
    "OpenAICompatLLM",
    "HttpMCPTransport",
    "StdioChannel",
    "StdioMCPTransport",
    "SubprocessStdioChannel",
    "HttpFetcher",
]
