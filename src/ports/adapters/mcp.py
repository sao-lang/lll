"""MCP 默认适配器（R5 §6 / 规则 9）：stdio 与 HTTP 两种真 JSON-RPC 2.0 客户端。

框架为 R5 的 MCPTransport 协议提供开箱可用的默认实现，兑现设计里"默认 stdio/http"
的承诺——此前只有协议 + MCPManager 门面 + 测试 fake，协议公开却连不上任何 server。

协议纪律：
- initialize 握手先行（JSON-RPC 2.0 + 协议版本 + 客户端身份），随后发 initialized 通知；
- tools/list 的 inputSchema 映射为 ToolSpec.parameters_schema；
- tools/call 取 content[].text 拼成返回值；ping 走标准 ping 方法；
- 错误（JSON-RPC error / HTTP 非 2xx / 通道故障）统一折叠为 RuntimeError，
  由上层 MCPManager/ToolRegistry 再折叠为 ToolResult，不裸抛穿。

IO 收敛：stdio 的子进程封装成 StdioChannel 协议（IO 只在进程适配器里），
HTTP 复用 httpx.AsyncClient（注入 MockTransport 即可测试，不联网）。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import httpx

from hooks import DangerLevel, ToolSpec
from runtime import DynamicValue

from harness.tools import MCPTransport

__all__ = [
    "MCP_CLIENT_NAME",
    "MCP_CLIENT_VERSION",
    "MCP_PROTOCOL_VERSION",
    "HttpMCPTransport",
    "StdioChannel",
    "StdioMCPTransport",
    "SubprocessStdioChannel",
]

MCP_PROTOCOL_VERSION = "2024-11-05"
"""MCP 协议版本（客户端握手声明；与 server 协商后以其返回为准）。"""
MCP_CLIENT_NAME = "agentkit"
MCP_CLIENT_VERSION = "5.0.0"


def _initialize_request(id: int) -> dict[str, DynamicValue]:
    """构造 initialize 请求帧（JSON-RPC 2.0 + 协议版本 + 客户端身份）。"""
    return {
        "jsonrpc": "2.0",
        "id": id,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
        },
    }


class _JsonRpcPeer:
    """JSON-RPC 请求帧构造与响应判定的共享逻辑（两种 transport 复用）。

    规则 7：私有 helper 一句话 docstring，说明它收敛的是"帧格式"而非 IO。
    """

    def __init__(self, danger: DangerLevel = DangerLevel.SAFE) -> None:
        """构造帧助手：danger 是 MCP 工具的默认风险级别（可被上层覆盖）。"""
        self._id = 0
        self._danger = danger
        self._ready = False

    def next_id(self) -> int:
        """自增请求 id（每帧唯一，用于匹配响应）。"""
        self._id += 1
        return self._id

    def request(self, method: str, params: dict[str, DynamicValue] | None = None) -> dict[str, DynamicValue]:
        """构造带 id 的请求帧；params 缺省时省略该键（ping 无需参数）。"""
        frame: dict[str, DynamicValue] = {
            "jsonrpc": "2.0", "id": self.next_id(), "method": method,
        }
        if params is not None:
            # 只有非空 params 才写入，避免给 ping 塞空对象引发某些 server 的严格校验。
            frame["params"] = params
        return frame

    def initialized_notification(self) -> dict[str, DynamicValue]:
        """构造 initialized 通知（无 id；握手第二步告知 server 客户端就绪）。"""
        return {"jsonrpc": "2.0", "method": "notifications/initialized"}

    def result_of(self, response: dict[str, DynamicValue]) -> DynamicValue:
        """从响应帧取 result；error 折叠为 RuntimeError，不裸抛字典。"""
        if not isinstance(response, dict):
            # 非 dict 帧是协议违约（如畸形 JSON 负载），显式报错让上层折叠。
            raise RuntimeError("MCP response frame malformed")
        if "error" in response:
            # JSON-RPC error 是协议级失败，转成异常让上层统一折叠为 ToolResult。
            error = response["error"]
            message = error.get("message", "unknown") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"MCP error: {message}")
        return response.get("result")

    def tools_from(self, result: DynamicValue) -> list[ToolSpec]:
        """把 tools/list 的 result 映射为 ToolSpec（inputSchema → parameters_schema）。"""
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            # 形态不符协议：显式报错，避免静默返回空目录让上层误以为无工具。
            raise RuntimeError("MCP tools/list result malformed")
        specs: list[ToolSpec] = []
        for raw in result["tools"]:  # 逐条归一化，单条畸形不影响其余工具。
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            schema = raw.get("inputSchema")
            specs.append(ToolSpec(
                name=raw["name"],
                description=str(raw.get("description", "")),
                parameters_schema=schema if isinstance(schema, dict) else {},
                danger=self._danger,
                source="mcp",
            ))
        return specs

    @staticmethod
    def text_of(result: DynamicValue) -> str:
        """把 tools/call 的 content[].text 拼成字符串（非 text 项跳过）。"""
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            return ""
        parts = [
            str(item["text"])
            for item in result["content"]
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item
        ]
        return "\n".join(parts)


@runtime_checkable
class StdioChannel(Protocol):
    """stdio 子进程通道（IO 边界；真实现用 asyncio 子进程，测试用内存 fake）。"""
    name: str

    async def send(self, message: dict[str, DynamicValue]) -> None:
        """向 server stdin 写一行 JSON 帧。"""
        ...

    async def recv(self) -> dict[str, DynamicValue]:
        """从 server stdout 读一行并解析为 JSON 帧。"""
        ...

    async def close(self) -> None:
        """关闭通道（终止子进程/释放管道）。"""
        ...


@runtime_checkable
class _ProcessHandle(Protocol):
    """子进程句柄的最小面（asyncio 进程与测试 fake 都满足）。"""

    async def write(self, data: bytes) -> None:
        """写 stdin。"""
        ...

    async def drain(self) -> None:
        """等待写入落盘。"""
        ...

    async def readline(self) -> bytes:
        """读 stdout 一行。"""
        ...

    def close(self) -> None:
        """关闭 stdin。"""
        ...

    async def wait(self) -> int:
        """等待进程结束。"""
        ...

    def terminate(self) -> None:
        """终止进程。"""
        ...


async def _spawn(command: list[str]) -> _ProcessHandle:
    """真 spawner：asyncio 起子进程（唯一允许的系统 IO 点，其余全注入可测）。"""
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # 进程对象的 stdin/stdout 非空（PIPE 模式）；cast 收敛为协议视图（规则 6 动态边界）。
    from typing import cast
    return cast(_ProcessHandle, proc)


class SubprocessStdioChannel:
    """真 stdio 通道：spawn 子进程、换行分隔 JSON 帧读写、关闭时终止。

    spawner 可注入（测试传脚本化句柄）；缺省用 asyncio 真起进程。
    """

    name = "mcp_subprocess"

    def __init__(
        self,
        command: list[str],
        *,
        spawner: Callable[[list[str]], Awaitable[_ProcessHandle]] | None = None,
    ) -> None:
        """构造：command 为 server 启动命令；spawner 缺省为真子进程工厂。"""
        self._command = command
        self._spawner = spawner or _spawn
        self._process: _ProcessHandle | None = None

    async def _ensure(self) -> _ProcessHandle:
        """首次 IO 时 spawn（懒启动，构造不产生副作用）。"""
        if self._process is None:
            self._process = await self._spawner(self._command)
        return self._process

    async def send(self, message: dict[str, Any]) -> None:
        """写一帧：JSON + 换行分隔（MCP stdio 的帧定界约定）。"""
        process = await self._ensure()
        await process.write(json.dumps(message).encode("utf-8") + b"\n")
        await process.drain()

    async def recv(self) -> dict[str, Any]:
        """读一帧：stdout 空行即 EOF，显式报错不伪装成功。"""
        process = await self._ensure()
        line = await process.readline()
        if not line:
            raise RuntimeError("MCP stdio EOF: server exited")
        payload: Any = json.loads(line)
        if not isinstance(payload, dict):
            raise RuntimeError("MCP stdio frame malformed")
        return payload

    async def close(self) -> None:
        """关闭 stdin 并终止子进程（幂等；未启动过则无需处理）。"""
        if self._process is None:
            return
        self._process.close()
        self._process.terminate()
        await self._process.wait()
        self._process = None


class StdioMCPTransport:
    """stdio MCP 客户端：经 StdioChannel 发 JSON-RPC，握手后列/调工具。

    属性：name: 适配器名（可插拔物留名纪律）。
    """

    name = "mcp_stdio"

    def __init__(self, channel: StdioChannel, *, danger: DangerLevel = DangerLevel.SAFE) -> None:
        """构造：注入 stdio 通道；danger 为拉回工具的默认风险级别。"""
        self._channel = channel
        self._peer = _JsonRpcPeer(danger)

    async def _handshake(self) -> None:
        """首次使用时握手：initialize → 校验响应 → 发 initialized 通知。"""
        if self._peer._ready:
            return
        req = _initialize_request(self._peer.next_id())
        await self._channel.send(req)
        response = await self._channel.recv()
        self._peer.result_of(response)  # error 直接抛，由调用路径折叠。
        await self._channel.send(self._peer.initialized_notification())
        self._peer._ready = True

    async def _roundtrip(self, method: str, params: dict[str, DynamicValue] | None = None) -> DynamicValue:
        """握手（若未握手）→ 发请求 → 收响应 → 取 result（统一错误折叠）。"""
        await self._handshake()
        await self._channel.send(self._peer.request(method, params))
        response = await self._channel.recv()
        return self._peer.result_of(response)

    async def list_tools(self, server: str) -> list[ToolSpec]:
        """发现工具：tools/list 的 inputSchema 映射为 ToolSpec（同 Registry 目录形态）。"""
        result = await self._roundtrip("tools/list")
        return self._peer.tools_from(result)

    async def call_tool(self, server: str, tool: str, args: dict[str, DynamicValue]) -> DynamicValue:
        """调用工具：tools/call 的 text 内容拼成返回值（形态由工具决定）。"""
        result = await self._roundtrip("tools/call", {"name": tool, "arguments": args})
        return self._peer.text_of(result)

    async def ping(self, server: str) -> bool:
        """健康检查：标准 ping 成功即 True；异常折叠为 False 不抛穿。"""
        try:
            await self._roundtrip("ping")
            return True
        except Exception:
            # health 语义：不可达即 False，异常在此消化（规则 8 异常不抛穿）。
            return False

    async def reconnect(self) -> None:
        """显式重连：关闭旧通道 + 复位握手状态（不自动重试 call，防副作用重复执行）。"""
        await self._channel.close()
        self._peer._ready = False

    async def aclose(self) -> None:
        """关闭底层 stdio 通道（进程/管道资源释放）。"""
        await self._channel.close()


class HttpMCPTransport:
    """HTTP MCP 客户端：POST JSON-RPC，支持 JSON 与 SSE（text/event-stream）响应。

    属性：name: 适配器名。endpoint: MCP HTTP 端点。
    """

    name = "mcp_http"

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
        danger: DangerLevel = DangerLevel.SAFE,
    ) -> None:
        """构造：endpoint 为 MCP RPC 地址；client 可注入（MockTransport 测试）。"""
        self._endpoint = endpoint
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._peer = _JsonRpcPeer(danger)

    async def _post(self, frame: dict[str, DynamicValue]) -> dict[str, DynamicValue]:
        """发一帧并解析响应：JSON 直接 loads，SSE 取首个 data: JSON 帧。"""
        response = await self._client.post(self._endpoint, json=frame)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"MCP HTTP {response.status_code}: {response.text[:200]}")
        # 通知（initialized）的 202/204 空体无 JSON 可解析，返回空帧即可（调用方不取 result）。
        if response.status_code in (202, 204) or not response.text.strip():
            return {}
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(response.text)
        payload = json.loads(response.text)
        if not isinstance(payload, dict):
            raise RuntimeError("MCP response malformed")
        return payload

    @staticmethod
    def _parse_sse(text: str) -> dict[str, DynamicValue]:
        """从 SSE 正文取第一条 data: 行的 JSON（MCP 逐消息一个 event）。"""
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:"):].strip())
                if isinstance(payload, dict):
                    return payload
        # 没有 data: 帧是协议违约，显式报错而非返回空字典。
        raise RuntimeError("MCP SSE has no data frame")

    async def _handshake(self) -> None:
        """首次握手：initialize → 校验 → initialized 通知（与 stdio 同语义）。"""
        if self._peer._ready:
            return
        response = await self._post(_initialize_request(self._peer.next_id()))
        self._peer.result_of(response)
        await self._post(self._peer.initialized_notification())
        self._peer._ready = True

    async def _roundtrip(self, method: str, params: dict[str, DynamicValue] | None = None) -> DynamicValue:
        """握手 → 发请求 → 解析 → 取 result。"""
        await self._handshake()
        response = await self._post(self._peer.request(method, params))
        return self._peer.result_of(response)

    async def list_tools(self, server: str) -> list[ToolSpec]:
        """发现工具：与 stdio 同构（同一帧助手，保证两种 transport 行为一致）。"""
        return self._peer.tools_from(await self._roundtrip("tools/list"))

    async def call_tool(self, server: str, tool: str, args: dict[str, DynamicValue]) -> DynamicValue:
        """调用工具：text 内容拼接（动态值由工具决定，交给上层信封承载）。"""
        return self._peer.text_of(await self._roundtrip("tools/call", {"name": tool, "arguments": args}))

    async def ping(self, server: str) -> bool:
        """健康检查：标准 ping，异常折叠为 False。"""
        try:
            await self._roundtrip("ping")
            return True
        except Exception:
            return False

    async def reconnect(self) -> None:
        """显式重连：复位握手状态，下次调用重新 initialize（不做副作用重放）。"""
        self._peer._ready = False

    async def aclose(self) -> None:
        """关闭自有的 httpx client（注入的 client 由调用方负责关闭）。"""
        if self._owns_client:
            await self._client.aclose()


# 履约校验：两个真实现必须满足 R5 的 MCPTransport 协议（装配期由 isinstance 校验，这里仅静态断言）。
_stdio_is_transport: type[MCPTransport] = StdioMCPTransport
_http_is_transport: type[MCPTransport] = HttpMCPTransport
