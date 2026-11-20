"""R5 工具包公共 API：装饰器、注册表、披露索引、MCP 门面与能力装配。

导出面即 tests/r5 契约；内部实现细节不进 __all__。
"""
from __future__ import annotations

from .capability import ToolsCapability
from .decorator import ToolFn, clear_pending, pending_tools, tool
from .execution import CodeExecTool, FileExecTool, ShellExecTool
from .index import ToolIndex
from .mcp import MCPManager, MCPTransport
from .registry import ToolRegistry
from .types import DangerLevel, SandboxPolicy, ToolSource
from .webfetch import WebFetcher, WebFetchTool

__all__ = [
    "CodeExecTool",
    "DangerLevel",
    "FileExecTool",
    "MCPManager",
    "MCPTransport",
    "SandboxPolicy",
    "ShellExecTool",
    "ToolFn",
    "ToolIndex",
    "ToolRegistry",
    "ToolSource",
    "ToolsCapability",
    "WebFetcher",
    "WebFetchTool",
    "clear_pending",
    "pending_tools",
    "tool",
]
