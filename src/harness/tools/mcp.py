"""MCP Manager：外部工具发现 → 命名空间注入统一 Registry（只注入 L1 目录）。

本地 @tool 工具与 MCP 工具在运行时无差别：同一 Registry、同一链路、同一 danger 分级。
"""
from __future__ import annotations

import inspect
from typing import Protocol

from ports import DangerLevel, PortCtx, ToolCall, ToolResult, ToolSpec
from runtime import DynamicValue

from .registry import ToolRegistry

__all__ = ["MCPTransport", "MCPManager"]


class MCPTransport(Protocol):
    """MCP 传输层协议：发现工具、调用工具、健康检查。"""

    async def list_tools(self, server: str) -> list[ToolSpec]:
        """列出服务器的工具。"""
        ...

    async def call_tool(self, server: str, tool: str, args: dict[str, DynamicValue], ctx: PortCtx) -> DynamicValue:
        """调用服务器上的工具。"""
        ...

    async def ping(self, server: str) -> bool:
        """探活；返回是否可达。"""
        ...


class MCPManager:
    """MCP 工具管理器：发现外部工具并注入统一 Registry（只注入 L1 目录）。"""

    name = "mcp-manager"  # 组件标识

    def __init__(self, registry: ToolRegistry, transport: MCPTransport) -> None:
        """装配注册表与传输层。"""
        self._registry = registry  # 目标注册表
        self._transport = transport  # MCP 传输实现
        self.servers: dict[str, list[str]] = {}  # server → 注入的工具名列表

    async def connect(self, server: str) -> list[ToolSpec]:
        """发现并注入工具：fqname = mcp__<server>__<tool>；完整 schema 只在 L2 按需取。"""
        specs = await self._transport.list_tools(server)
        injected: list[ToolSpec] = []
        for spec in specs:
            fqname = f"mcp__{server}__{spec.name}"
            local_name = spec.name

            async def invoke(args: dict[str, DynamicValue], ctx: PortCtx,
                             s: str = server, t: str = local_name) -> DynamicValue:
                """闭包工具实现：转发调用到 MCP 服务器（默认参数锁定 server/tool）。"""
                out = self._transport.call_tool(s, t, args, ctx)
                return await out if inspect.isawaitable(out) else out

            remote = spec.model_copy(update={
                "name": fqname,
                "source": "mcp",
                "danger": spec.danger or DangerLevel.SAFE,
            })
            self._registry.register(remote, invoke)
            injected.append(remote)
        self.servers[server] = [s.name for s in injected]
        return injected

    async def call(self, fqname: str, args: dict[str, DynamicValue], ctx: PortCtx | None = None) -> ToolResult:
        """与本地工具同一入口：走 Registry → danger 路由 → 链路。"""
        return await self._registry.call(
            ToolCall(call_id=f"mcp:{fqname}", name=fqname, args=args),
            ctx or PortCtx("mcp"),
        )

    async def health(self) -> dict[str, bool]:
        """探活所有已连接服务器，返回 server → 可达性字典。"""
        return {server: await self._transport.ping(server) for server in self.servers}
