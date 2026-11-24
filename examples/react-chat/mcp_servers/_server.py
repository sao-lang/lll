"""最小 MCP server 骨架（标准库 JSON-RPC over stdio，履约 MCP 2024-11-05）。

为什么手写而非装 mcp SDK：框架 ports/adapters/mcp.py 已有完整客户端 transport，
examples 层只需一个能握手、列工具、调工具的最小 server；零新增依赖。
IO（httpx/urllib）只在 examples MCP server 层，不进 src/harness（红线规则 8）。

子类覆写 TOOLS 与 call(tool, args) 即可。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    """标准库 MCP server：逐行读请求帧，逐行写响应帧。"""

    def __init__(self, name: str, tools: list[dict[str, Any]]) -> None:
        self.name = name
        self.tools = tools
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.ready = False

    def _initialize(self, req: dict[str, Any]) -> dict[str, Any]:
        self.ready = True
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": "1.0.0"}}

    def _list_tools(self) -> dict[str, Any]:
        return {"tools": self.tools}

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """处理一帧：返回响应帧或 None（通知无响应）。"""
        method = req.get("method", "")
        rid = req.get("id")
        if method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                result = self._initialize(req)
            elif not self.ready:
                raise RuntimeError("server not initialized")
            elif method == "tools/list":
                result = self._list_tools()
            elif method == "tools/call":
                params = req.get("params") or {}
                tool = str(params.get("name", ""))
                handler = self.handlers.get(tool)
                if handler is None:
                    raise RuntimeError(f"unknown tool: {tool}")
                result = handler(params.get("arguments") or {})
            elif method == "ping":
                result = {}
            else:
                raise RuntimeError(f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as exc:  # noqa: BLE001 - 折叠成 JSON-RPC error 帧
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32000, "message": str(exc)}}

    def run(self) -> None:
        """stdio 主循环（逐行 JSON）。"""
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def text_result(text: str) -> dict[str, Any]:
    """tools/call 的标准返回：content[].type=text。"""
    return {"content": [{"type": "text", "text": text}]}
