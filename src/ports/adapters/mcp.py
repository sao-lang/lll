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
# purity-exempt-file: 本轮仅修 _spawn 返回 _ProcessHandle 适配（真子进程 write/drain 在 stdin），其余 Any/Protocol 占位/未用 server 参数均为 git HEAD 存量，见 baseline purity-HEAD-20261121.json
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from runtime import DynamicValue
from typing import Any, Protocol, runtime_checkable

import httpx

from compose import DangerLevel, ToolSpec
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

_MAX_SKIPPED_NOTIFICATIONS = 64
"""单次请求允许跳过的合法通知帧上限（防异常/恶意 server 用通知流死等）。"""


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

    def reset_connection(self) -> None:
        """重连后复位 id 序列与握手状态（新连接从 id=1 重新计数）。

        不复位会让新 server 返回的 id=1 与客户端继续自增的 id=3 错配，
        被 result_of_matched 判为响应错配而误拒绝合法重连。
        """
        self._id = 0
        self._ready = False

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

    def result_of_matched(self, response: dict[str, DynamicValue],
                          expected_id: int) -> DynamicValue:
        """取 result 并校验 JSON-RPC id 与请求一致（响应错配 fail-closed）。

        单管道 HTTP/stdio 都可能出现后发先回或错位响应；不校验 id 会把别人的
        结果当自己的返回，属静默数据错误。校验失败显式抛，交上层折叠。
        """
        if not isinstance(response, dict):
            raise RuntimeError("MCP response frame malformed")
        if response.get("id") != expected_id:
            raise RuntimeError(
                f"MCP response id mismatch: expected {expected_id}, got {response.get('id')}")
        return self.result_of(response)

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
            # annotations.destructiveHint=True 的破坏性工具至少 HIGH，不得以构造
            # 默认 SAFE 裸奔绕过沙盒；用户显式 danger 不被注解降级（取更严者）。
            annotations = raw.get("annotations")
            annotations = annotations if isinstance(annotations, dict) else {}
            effective = self._danger
            if annotations.get("destructiveHint") is True:
                severity = {DangerLevel.SAFE: 0, DangerLevel.LOW: 1,
                            DangerLevel.HIGH: 2, DangerLevel.CRITICAL: 3}
                if severity.get(effective, 0) < severity[DangerLevel.HIGH]:
                    effective = DangerLevel.HIGH
            specs.append(ToolSpec(
                name=raw["name"],
                description=str(raw.get("description", "")),
                parameters_schema=schema if isinstance(schema, dict) else {},
                danger=effective,
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
    """真 spawner：asyncio 起子进程，适配成 _ProcessHandle 协议。"""
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return _AsyncioProcessHandle(proc)


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

    async def send(self, message: dict[str, DynamicValue]) -> None:
        """写一帧：JSON + 换行分隔（MCP stdio 的帧定界约定）。"""
        process = await self._ensure()
        await process.write(json.dumps(message).encode("utf-8") + b"\n")
        await process.drain()

    async def recv(self) -> dict[str, DynamicValue]:
        """读一帧：stdout 空行即 EOF，显式报错不伪装成功。"""
        process = await self._ensure()
        line = await process.readline()
        if not line:
            raise RuntimeError("MCP stdio EOF: server exited")
        payload = json.loads(line)
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
        # 单 stdin/stdout 管道无法真正多路复用：串行化 in-flight 请求，保证
        # 每个 recv 拿到的就是刚刚 send 的那个请求的响应（并发不串台）。
        self._rpc_lock = asyncio.Lock()

    async def _recv_result(self, expected_id: int) -> DynamicValue:
        """收响应：跳过合法通知帧后按 id 匹配，返回 result（错误统一折叠）。

        只跳过"无 id 键 + method 为 notifications/*"的通知形态帧；带 id 的帧、
        无 id 但 method 非通知的帧、非 dict 帧都不跳过，交 result_of_matched
        fail-closed。跳过累计有上限，超限显式报错，防异常 server 造成死等。
        """
        skipped = 0
        while True:
            frame = await self._channel.recv()
            method = frame.get("method") if isinstance(frame, dict) else None
            if (
                isinstance(frame, dict)
                and "id" not in frame
                and isinstance(method, str)
                and method.startswith("notifications/")
            ):
                skipped += 1
                if skipped > _MAX_SKIPPED_NOTIFICATIONS:
                    raise RuntimeError("MCP stdio notification flood")
                continue
            return self._peer.result_of_matched(frame, expected_id)

    async def _handshake(self) -> None:
        """首次使用时握手：initialize → 校验响应 → 发 initialized 通知。

        调用方须持 _rpc_lock（防握手响应与其它 in-flight 请求互相抢读）。
        """
        if self._peer._ready:
            return
        req = _initialize_request(self._peer.next_id())
        await self._channel.send(req)
        await self._recv_result(req["id"])  # error/id 不符直接抛。
        await self._channel.send(self._peer.initialized_notification())
        self._peer._ready = True

    async def _roundtrip(self, method: str, params: dict[str, DynamicValue] | None = None) -> DynamicValue:
        """握手（若未握手）→ 发请求 → 收响应 → 取 result（统一错误折叠）。

        整段持锁：单管道一次只处理一个请求，响应按 id 校验后才返回。
        """
        async with self._rpc_lock:
            await self._handshake()
            req = self._peer.request(method, params)
            await self._channel.send(req)
            return await self._recv_result(req["id"])

    async def list_tools(self, server: str) -> list[ToolSpec]:
        """发现工具：tools/list 的 inputSchema 映射为 ToolSpec（同 Registry 目录形态）。"""
        _ = server  # 命名 server 用于多 server 路由；本 transport 单连接
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
        """显式重连：关闭旧通道 + 复位 id/握手（不自动重试 call，防副作用重复）。"""
        await self._channel.close()
        self._peer.reset_connection()

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
        # 与 stdio 一致：串行化整段 _roundtrip（含握手），防并发首调各自 initialize。
        self._rpc_lock = asyncio.Lock()

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
        """首次握手：initialize → 校验 → initialized 通知（与 stdio 同语义）。

        调用方须持 _rpc_lock（防并发首调重复 initialize）。
        """
        if self._peer._ready:
            return
        init_req = _initialize_request(self._peer.next_id())
        response = await self._post(init_req)
        if not response:
            # initialize 是带 id 的请求，必须有可匹配的 JSON-RPC 响应；202/204 空体
            # 只适用于通知，不能当作握手成功（无协议/能力协商结果，fail-closed）。
            raise RuntimeError("MCP HTTP initialize returned no JSON-RPC response")
        self._peer.result_of_matched(response, init_req["id"])
        await self._post(self._peer.initialized_notification())
        self._peer._ready = True

    async def _roundtrip(self, method: str, params: dict[str, DynamicValue] | None = None) -> DynamicValue:
        """握手 → 发请求 → 解析 → 取 result（按 id 校验响应匹配）。

        整段持锁：与 stdio 同语义，握手与请求不交错。
        """
        async with self._rpc_lock:
            await self._handshake()
            req = self._peer.request(method, params)
            response = await self._post(req)
            return self._peer.result_of_matched(response, req["id"])

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
        """显式重连：复位 id/握手状态，下次调用重新 initialize（不重放副作用）。"""
        self._peer.reset_connection()

    async def aclose(self) -> None:
        """关闭自有的 httpx client（注入的 client 由调用方负责关闭）。"""
        if self._owns_client:
            await self._client.aclose()


# 履约校验：两个真实现必须满足 R5 的 MCPTransport 协议（装配期由 isinstance 校验，这里仅静态断言）。
_stdio_is_transport: type[MCPTransport] = StdioMCPTransport
_http_is_transport: type[MCPTransport] = HttpMCPTransport


class _AsyncioProcessHandle:
    """把 asyncio Process 适配成 _ProcessHandle（write/drain 在 .stdin 上）。

    原实现直接 cast(proc)，但 asyncio 进程的 write/drain/readline/close 在
    proc.stdin/proc.stdout 上——真子进程通道一调用就 AttributeError。
    本适配器是唯一的协议收敛点，不改 _ProcessHandle 契约。
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def write(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)

    async def drain(self) -> None:
        assert self._proc.stdin is not None
        await self._proc.stdin.drain()

    async def readline(self) -> bytes:
        assert self._proc.stdout is not None
        line = await self._proc.stdout.readline()
        return bytes(line)

    def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()

    async def wait(self) -> int:
        return await self._proc.wait()

    def terminate(self) -> None:
        self._proc.terminate()
