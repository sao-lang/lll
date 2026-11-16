"""M4 工具系统：@tool / ToolRegistry / ToolIndex / MCPManager。"""
from ports import DangerLevel

from .decorator import SandboxPolicy, ToolDefinition, ToolDefinitionError, tool
from .index import SkillProvider, ToolIndex, ToolIndexEntry
from .mcp import MCPManager, MCPTransport
from .registry import ToolEntry, ToolRegistry

__all__ = [
    "tool", "ToolDefinition", "ToolDefinitionError", "SandboxPolicy", "DangerLevel",
    "ToolRegistry", "ToolEntry", "ToolIndex", "ToolIndexEntry", "SkillProvider",
    "MCPManager", "MCPTransport",
]
