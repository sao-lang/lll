"""MCP 默认适配器：stdio/http 真 JSON-RPC 客户端（R5-T04 真实后端接线）。

不联网/不起真进程：stdio 用注入的假子进程通道，http 用 httpx.MockTransport。
验证 initialize 握手、tools/list 映射、tools/call、ping 均走真协议帧。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from hooks import DangerLevel, PortCtx, ToolSpec
from ports.adapters.mcp import (
    HttpMCPTransport, StdioMCPTransport, StdioChannel,
)


def _ctx() -> PortCtx:
    return PortCtx(session_id="s", run_id="r")


# ---- 共享 JSON-RPC 帧构造测试 ----

def test_initialize_uses_jsonrpc_2_and_handshake() -> None:
    """initialize 用 JSON-RPC 2.0 且带协议版本/客户端信息（真 MCP 握手第一步）。"""
    from ports.adapters.mcp import _initialize_request
    req = _initialize_request(id=1)
    assert req["jsonrpc"] == "2.0" and req["id"] == 1
    assert req["method"] == "initialize"
    params = req["params"]
    assert "protocolVersion" in params and "clientInfo" in params


def test_protocol_constants_are_stable() -> None:
    """协议版本与客户端身份是稳定常量，不散落魔法串。"""
    from ports.adapters.mcp import MCP_PROTOCOL_VERSION, MCP_CLIENT_NAME
    assert MCP_PROTOCOL_VERSION and MCP_CLIENT_NAME


# ---- stdio transport ----

class FakeStdioChannel:
    """内存子进程通道：send 记录请求，recv 按序返回预设响应（含 initialize）。"""
    name = "fake_stdio_channel"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, message: dict[str, object]) -> None:
        self.sent.append(message)

    async def recv(self) -> dict[str, object]:
        if not self.responses:
            raise RuntimeError("no more responses")
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def _stdio_responses() -> list[dict[str, object]]:
    """标准握手 → 列工具 → 调用 → ping 的响应序列。"""
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"},
        }},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
            "name": "search", "description": "搜索网页",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }]}},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "hit"}], "isError": False}},
        {"jsonrpc": "2.0", "id": 4, "result": {}},
    ]


def test_stdio_channel_protocol_satisfied() -> None:
    assert isinstance(FakeStdioChannel([]), StdioChannel)


def test_stdio_handshake_and_list_tools() -> None:
    """stdio：先 initialize 再 tools/list；inputSchema 映射为 ToolSpec.parameters_schema。"""
    channel = FakeStdioChannel(_stdio_responses())
    transport = StdioMCPTransport(channel)
    specs = asyncio.run(transport.list_tools("srv"))
    assert [s.name for s in specs] == ["search"]
    assert specs[0].parameters_schema["properties"]["q"]["type"] == "string"
    assert specs[0].danger is DangerLevel.SAFE and specs[0].source == "mcp"
    methods = [m["method"] for m in channel.sent]
    assert methods[0] == "initialize" and "notifications/initialized" in methods
    assert "tools/list" in methods


def test_stdio_call_tool_and_ping() -> None:
    """stdio：tools/call 取 text content 拼成值；ping 走真 ping 方法。"""
    channel = FakeStdioChannel(_stdio_responses())
    transport = StdioMCPTransport(channel)
    asyncio.run(transport.list_tools("srv"))  # 完成握手
    assert asyncio.run(transport.call_tool("srv", "search", {"q": "x"})) == "hit"
    assert asyncio.run(transport.ping("srv")) is True
    methods = [m["method"] for m in channel.sent]
    assert "tools/call" in methods and "ping" in methods


def test_stdio_initialize_only_once() -> None:
    """重连纪律：同一 transport 只握手一次，后续调用复用会话。"""
    channel = FakeStdioChannel(_stdio_responses())
    transport = StdioMCPTransport(channel)
    asyncio.run(transport.list_tools("srv"))
    asyncio.run(transport.ping("srv"))
    assert [m["method"] for m in channel.sent].count("initialize") == 1


def test_stdio_error_response_is_folded() -> None:
    """JSON-RPC error 响应折叠为 RuntimeError（由 MCPManager/Registry 再折叠为 ToolResult）。"""
    channel = FakeStdioChannel([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "no tools"}},
    ])
    transport = StdioMCPTransport(channel)
    with pytest.raises(RuntimeError, match="no tools"):
        asyncio.run(transport.list_tools("srv"))


def test_stdio_close_marks_channel_closed() -> None:
    """close 关闭底层通道（资源不泄漏）。"""
    channel = FakeStdioChannel(_stdio_responses())
    transport = StdioMCPTransport(channel)
    asyncio.run(transport.aclose())
    assert channel.closed is True


# ---- http transport ----

def _http_handler(responses: list[dict[str, object]], captured: list[dict[str, object]]) -> Callable[[httpx.Request], httpx.Response]:
    """把预设 JSON-RPC 响应按序返回，并捕获 POST body（JSON 模式）。"""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        captured.append(frame)
        if "id" not in frame:
            # 通知（initialized）不期待响应：真 MCP server 回 202 空体，不消耗响应队列。
            return httpx.Response(202, text="")
        if not queue:
            return httpx.Response(500, text="no more")
        return httpx.Response(200, json=queue.pop(0))

    return handler


def test_http_handshake_and_list_tools() -> None:
    """http：POST JSON-RPC、initialize 握手、tools/list 映射。"""
    captured: list[dict[str, object]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_http_handler(_stdio_responses(), captured)))
    transport = HttpMCPTransport("https://mcp.example/rpc", client=client)
    specs = asyncio.run(transport.list_tools("srv"))
    assert [s.name for s in specs] == ["search"]
    methods = [m["method"] for m in captured]
    assert methods[0] == "initialize" and "tools/list" in methods
    assert client is not None


def test_http_call_tool_and_ping() -> None:
    """http：tools/call 与 ping 均走真协议；关闭 client 由调用方负责。"""
    captured: list[dict[str, object]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_http_handler(_stdio_responses(), captured)))
    transport = HttpMCPTransport("https://mcp.example/rpc", client=client)
    asyncio.run(transport.list_tools("srv"))
    assert asyncio.run(transport.call_tool("srv", "search", {"q": "x"})) == "hit"
    assert asyncio.run(transport.ping("srv")) is True
    assert "tools/call" in [m["method"] for m in captured]


def test_http_sse_response_parsed() -> None:
    """http 支持 SSE（text/event-stream）：从 data: 行提取 JSON-RPC 响应。"""
    turns = 0
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turns
        frame = json.loads(request.content)
        captured.append(frame)
        if "id" not in frame:
            # 通知回 202 空体，不占 SSE 响应帧（与真实 MCP 语义一致）。
            return httpx.Response(202, text="")
        turns += 1
        if turns == 1:
            result = {"protocolVersion": "x", "capabilities": {}}
        else:
            result = {"tools": [{"name": "remote", "description": "d", "inputSchema": {"type": "object"}}]}
        event = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': turns, 'result': result})}\n\n"
        return httpx.Response(200, text=event, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpMCPTransport("https://mcp.example/sse", client=client)
    specs = asyncio.run(transport.list_tools("srv"))
    assert [s.name for s in specs] == ["remote"]


def test_http_non_2xx_folded() -> None:
    """HTTP 非 2xx 折叠为 RuntimeError，不裸抛 httpx 异常。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(503, text="unavailable")
    ))
    transport = HttpMCPTransport("https://mcp.example/rpc", client=client)
    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(transport.list_tools("srv"))


def test_transport_protocol_satisfied() -> None:
    """两个真实现履约 R5 MCPTransport 协议（isinstance）。"""
    from harness.tools import MCPTransport
    channel = FakeStdioChannel(_stdio_responses())
    assert isinstance(StdioMCPTransport(channel), MCPTransport)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_http_handler(_stdio_responses(), [])))
    assert isinstance(HttpMCPTransport("https://x", client=client), MCPTransport)


# ---- 真子进程通道（实际 spawn MCP server）----

class FakeProcess:
    """假子进程句柄：脚本化 stdin/stdout，验证真通道的帧读写与关闭。"""
    name = "fake_process"

    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.stdout_lines = list(stdout_lines)
        self.written: list[bytes] = []
        self.closed = False
        self.terminated = False

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    async def readline(self) -> bytes:
        if not self.stdout_lines:
            return b""
        return self.stdout_lines.pop(0)

    def close(self) -> None:
        self.closed = True

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True


def test_subprocess_channel_spawns_and_frames(tmp_path) -> None:
    """真通道：spawn 命令 → 发换行分隔 JSON → 读响应行 → close 终止进程。"""
    from ports.adapters.mcp import SubprocessStdioChannel

    proc = FakeProcess([b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])
    spawned: list[list[str]] = []

    async def spawner(command: list[str]) -> FakeProcess:
        spawned.append(command)
        return proc

    channel = SubprocessStdioChannel(["mcp-server"], spawner=spawner)
    asyncio.run(channel.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
    response = asyncio.run(channel.recv())
    asyncio.run(channel.close())

    assert spawned == [["mcp-server"]]
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert proc.written and proc.written[0].endswith(b"\n")
    assert proc.closed is True and proc.terminated is True


def test_subprocess_channel_eof_raises(tmp_path) -> None:
    """server stdout EOF（进程退出）→ 显式报错，不返回空帧伪装成功。"""
    from ports.adapters.mcp import SubprocessStdioChannel

    proc = FakeProcess([])

    async def spawner(command: list[str]) -> FakeProcess:
        return proc

    channel = SubprocessStdioChannel(["mcp-server"], spawner=spawner)
    try:
        asyncio.run(channel.recv())
    except RuntimeError as e:
        assert "EOF" in str(e)
    else:
        raise AssertionError("EOF 应报错")


def test_real_stdio_transport_end_to_end() -> None:
    """真通道 + 真 transport：从 spawn 到 tools/list 全流程（无真进程，注入脚本化句柄）。"""
    from ports.adapters.mcp import StdioMCPTransport, SubprocessStdioChannel

    lines = [
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{}}}\n',
        b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"t","description":"d","inputSchema":{"type":"object"}}]}}\n',
    ]
    proc = FakeProcess(lines)

    async def spawner(command: list[str]) -> FakeProcess:
        return proc

    channel = SubprocessStdioChannel(["mcp-server"], spawner=spawner)
    transport = StdioMCPTransport(channel)
    specs = asyncio.run(transport.list_tools("srv"))
    assert [s.name for s in specs] == ["t"]


# ---- 帧/响应畸形与资源释放的防御分支 ----

def test_peer_tools_from_malformed_and_skips_bad_items() -> None:
    """tools/list 形态错 → 报错；单条畸形跳过不影响其余工具。"""
    from ports.adapters.mcp import _JsonRpcPeer
    peer = _JsonRpcPeer()
    try:
        peer.tools_from({"no_tools": []})
    except RuntimeError:
        pass
    else:
        raise AssertionError("畸形 result 应报错")
    specs = peer.tools_from({"tools": [
        {"name": "ok", "inputSchema": {"type": "object"}},
        {"description": "缺 name"},
        "not-a-dict",
    ]})
    assert [s.name for s in specs] == ["ok"]


def test_peer_text_of_non_list_returns_empty() -> None:
    """tools/call 非 content 列表 → 空串（上层仍包成成功信封）。"""
    from ports.adapters.mcp import _JsonRpcPeer
    assert _JsonRpcPeer.text_of({"unexpected": 1}) == ""


def test_stdio_malformed_frame_raises() -> None:
    """stdio 收到非 dict 帧 → 显式报错。"""
    from ports.adapters.mcp import StdioMCPTransport
    channel = FakeStdioChannel([["not", "a", "dict"]])  # type: ignore[list-item]
    transport = StdioMCPTransport(channel)
    try:
        asyncio.run(transport.list_tools("srv"))
    except RuntimeError as e:
        assert "frame malformed" in str(e) or "MCP error" in str(e)
    else:
        raise AssertionError("畸形帧应报错")


def test_stdio_close_without_start_is_noop() -> None:
    """通道未启动过就 close → 幂等 no-op，不报错。"""
    from ports.adapters.mcp import SubprocessStdioChannel
    channel = SubprocessStdioChannel(["mcp"])
    asyncio.run(channel.close())


def test_stdio_ping_folds_exception() -> None:
    """stdio ping 通道异常 → False（health 语义不抛穿）。"""
    from ports.adapters.mcp import StdioMCPTransport

    class BoomChannel(FakeStdioChannel):
        async def recv(self) -> dict[str, object]:
            raise RuntimeError("boom")

    transport = StdioMCPTransport(BoomChannel([]))
    assert asyncio.run(transport.ping("srv")) is False


def test_http_non_dict_response_raises() -> None:
    """HTTP 响应非 dict → 显式报错。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text='[1,2,3]')
    ))
    from ports.adapters.mcp import HttpMCPTransport
    transport = HttpMCPTransport("https://x", client=client)
    try:
        asyncio.run(transport.list_tools("srv"))
    except (RuntimeError, ValueError):
        pass
    else:
        raise AssertionError("非 dict 响应应报错")


def test_http_sse_without_data_frame_raises() -> None:
    """SSE 无 data: 行 → 显式报错。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="event: message\n\n",
                                   headers={"content-type": "text/event-stream"})
    ))
    from ports.adapters.mcp import HttpMCPTransport
    transport = HttpMCPTransport("https://x", client=client)
    try:
        asyncio.run(transport.list_tools("srv"))
    except RuntimeError as e:
        assert "no data frame" in str(e)
    else:
        raise AssertionError("无 data 帧应报错")


def test_http_ping_folds_exception() -> None:
    """HTTP ping 网络失败 → False。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(500, text="down")
    ))
    from ports.adapters.mcp import HttpMCPTransport
    transport = HttpMCPTransport("https://x", client=client)
    assert asyncio.run(transport.ping("srv")) is False


def test_http_owns_client_closes() -> None:
    """未注入 client 时 transport 自持 client，aclose 负责释放。"""
    from ports.adapters.mcp import HttpMCPTransport
    transport = HttpMCPTransport("https://x")  # 默认自建 client
    asyncio.run(transport.aclose())


# ---- G10：显式重连（不做自动重试，避免 tools/call 副作用重复执行）----

def test_stdio_reconnect_rehandshakes() -> None:
    """显式 reconnect：重置握手状态并重新 initialize（不自动重试 call，防副作用重复）。"""
    from ports.adapters.mcp import StdioMCPTransport, SubprocessStdioChannel

    batch1 = [
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"x","capabilities":{}}}\n',
        b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n',
    ]
    batch2 = [
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"x","capabilities":{}}}\n',
        b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"after","description":"d","inputSchema":{}}]}}\n',
    ]
    proc = FakeProcess(batch1)

    async def spawner(command: list[str]) -> FakeProcess:
        # 重连时替换为新进程句柄（旧进程关闭后 spawner 再给一份新响应）。
        nonlocal proc
        if proc.terminated:
            proc = FakeProcess(batch2)
        return proc

    channel = SubprocessStdioChannel(["mcp"], spawner=spawner)
    transport = StdioMCPTransport(channel)
    assert asyncio.run(transport.list_tools("srv")) == []

    asyncio.run(transport.reconnect())
    specs = asyncio.run(transport.list_tools("srv"))
    assert [s.name for s in specs] == ["after"]


def test_http_reconnect_rehandshakes() -> None:
    """HTTP reconnect：握手状态复位，下一次调用重新 initialize。"""
    from ports.adapters.mcp import HttpMCPTransport

    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        captured.append(frame)
        if "id" not in frame:
            return httpx.Response(202, text="")
        if frame["method"] == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": frame["id"],
                "result": {"protocolVersion": "x", "capabilities": {}},
            })
        # tools/list：每次都返回一份工具（重连后仍需可服务）。
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": frame["id"],
            "result": {"tools": [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpMCPTransport("https://x", client=client)
    asyncio.run(transport.list_tools("srv"))
    assert [m["method"] for m in captured].count("initialize") == 1

    asyncio.run(transport.reconnect())
    asyncio.run(transport.list_tools("srv"))
    assert [m["method"] for m in captured].count("initialize") == 2
