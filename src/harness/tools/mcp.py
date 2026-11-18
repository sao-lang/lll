"""R5 MCP 合并：本地传输协议 MCPTransport 与 MCPPort 实现 MCPManager。

MCP 工具以 mcp__<server>__<tool> 全名注入同一 ToolRegistry，
使 catalog/call 与本地工具无差别；health 走 transport.ping。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from hooks import MCPPort, PortCtx, ToolSpec
from runtime import DynamicValue

from .registry import ToolRegistry
from .types import ToolSource

__all__ = ["MCPTransport", "MCPManager"]


@runtime_checkable
class MCPTransport(Protocol):
    """**用户实现自定义 MCP 客户端的接口**（框架唯一的 MCP 客户端扩展点）。

    只需实现本协议的三个方法 + name 属性，就能把自己的 MCP 客户端接进框架：
    `MCPManager(registry, 你的客户端)`。框架自带的 stdio/http 实现也遵守本协议。

    三方法契约（实现前必读）：
        list_tools(server)：返回该 server 的工具清单。**工具名用原始名**，不要加
            `mcp__` 前缀——前缀由 MCPManager.connect 统一加，你重复加会导致双前缀。
            ToolSpec 需填 name/description/parameters_schema/danger/source；
            danger 必填（SAFE/LOW/HIGH/CRITICAL），它决定后续沙盒/审批路由。
        call_tool(server, tool, args)：tool 是原始名；args 为参数字典。返回值形态由
            工具决定（DynamicValue），框架会包成 ToolResult。异常不要吞，抛出后由
            上层折叠为失败信封。
        ping(server)：可达性探测，返回 bool；不要抛异常（health 会折叠，但返回
            False 更直接）。

    与 MCPPort 的分工：MCPPort（R2，带 ctx）是框架消费的能力门面，由 MCPManager 实现；
    本协议是**传输层**，不带 ctx，由用户/内置适配器实现。两者别混。

    如何验证实现合规：`isinstance(你的客户端, MCPTransport)` 为 True（runtime_checkable）。
    """

    name: str

    async def list_tools(self, server: str) -> list[ToolSpec]:
        """列出 server 暴露的工具（原始名，未加前缀）。"""
        ...

    async def call_tool(
        self, server: str, tool: str, args: dict[str, DynamicValue],
    ) -> DynamicValue:
        """调用工具，返回形态由工具决定（动态边界）。"""
        ...

    async def ping(self, server: str) -> bool:
        """探测 server 可达性。"""
        ...


class MCPManager:
    """MCP 能力包：把外部工具注入 registry，并提供带 ctx 的 MCPPort 门面。"""

    name: str = "mcp"
    _registry: ToolRegistry
    _transport: MCPTransport
    _servers: list[str]

    def __init__(self, registry: ToolRegistry, transport: MCPTransport) -> None:
        """构造门面：registry 为注入目标，transport 为底层 MCP 客户端。"""
        self._registry = registry
        self._transport = transport
        self._servers = []

    def _invoker(self, server: str, tool_name: str) -> Callable[..., DynamicValue]:
        """构造绑定到 (server, tool_name) 的调用闭包（参数绑定避免循环晚绑定）。"""
        async def invoke(**args: DynamicValue) -> DynamicValue:
            # 为何：transport 签名不含 ctx，这里只转发参数；ctx 由上层 port 负责。
            return await self._transport.call_tool(server, tool_name, args)

        return invoke

    async def connect(self, server: str, ctx: PortCtx) -> list[ToolSpec]:
        """发现并注入 server 的工具，返回加前缀后的 spec。

        参数：
            server: server 标识。ctx: port 调用上下文（本协议不需要，保留签名对齐）。
        返回：
            已注入 registry 的全名 ToolSpec 列表。
        """
        if server not in self._servers:
            # 记录已连接 server 供 health 使用；重复 connect 不重复记账。
            self._servers.append(server)
        specs = await self._transport.list_tools(server)
        injected: list[ToolSpec] = []
        for orig in specs:  # 逐条改全名、改来源并注册，保持 transport 返回顺序。
            fqname = f"mcp__{server}__{orig.name}"
            spec = orig.model_copy(
                update={"name": fqname, "source": ToolSource.MCP.value}
            )
            self._registry.register(spec, self._invoker(server, orig.name))
            injected.append(spec)
        return injected

    async def list_tools(self, server: str, ctx: PortCtx) -> list[ToolSpec]:
        """委托 transport 列出原始工具（带 ctx 门面，不改全名）。"""
        return await self._transport.list_tools(server)

    async def call_tool(
        self, server: str, tool: str, args: dict[str, DynamicValue], ctx: PortCtx,
    ) -> DynamicValue:
        """委托 transport 调用原始工具（带 ctx 门面）。"""
        return await self._transport.call_tool(server, tool, args)

    async def ping(self, server: str, ctx: PortCtx) -> bool:
        """委托 transport 探测 server 可达性。"""
        return await self._transport.ping(server)

    async def health(self, ctx: PortCtx) -> dict[str, bool]:
        """对所有已连接 server 逐一 ping，返回可达性字典。"""
        result: dict[str, bool] = {}
        for server in self._servers:  # 逐个探测，单点异常记为 False 而非中断整体健康检查。
            try:
                result[server] = await self._transport.ping(server)
            except Exception:
                # ping 失败即视为不可达，健康接口不抛穿到调用方。
                result[server] = False
        return result
