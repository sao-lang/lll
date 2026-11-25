"""补充的真实 e2e：MCP stdio / WebFetch HTTP / stream_events / checkpoint+审计 / runtime 导出。

纪律：真调用 + 可观察副作用；真 server 必须真起进程/真起 HTTP server，不用内存假
transport 冒充；每用例独立 loop（conftest 的 loop + run）；不靠 isinstance/callable/
is not None 自证，断言全部落到精确值或真实副作用。

每个用例 docstring 统一结构：输入 → 真实入口 → 精确断言 → 失败判据。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from compose import DangerLevel, PortCtx, StreamEventKind, Usage
from conftest import assert_no_secret, run
from runtime import AgentState, InMemoryEventStore, Invoke, Session

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. MCP 真本地 stdio server（真子进程 + 框架真 transport + MCPManager）
# ---------------------------------------------------------------------------
def test_infra_mcp01_real_stdio_subprocess_server(
    loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入=examples/react-chat/mcp_servers/weather_server.py 真子进程 + CHAT_OFFLINE=1；
    真实入口=MCPManager.connect/list_tools/call_tool/health 走 StdioMCPTransport+
    SubprocessStdioChannel 的逐行 JSON-RPC 真握手真调用；
    精确断言=注入工具全名 mcp__weather__get_weather、list_tools 原始名 get_weather、
    tools/call 返回真实 offline JSON（city=深圳/status=offline/固定 message）、
    registry 注入后可真调、ping=True；
    失败判据=子进程 EOF/超时/OSError 属环境阻断则显式 skip 并带真实异常文本；
    断言不符（工具名/正文/调用值错）为 FAIL，不用 SpyMCPTransport 冒充。"""
    from compose import ToolCall
    from harness.tools import MCPManager, ToolRegistry
    from ports.adapters.mcp import StdioMCPTransport, SubprocessStdioChannel

    # 离线开关让真 server 走确定性分支，避免外网；结果仍由真子进程产生，不是桩。
    monkeypatch.setenv("CHAT_OFFLINE", "1")
    server = ROOT / "examples" / "react-chat" / "mcp_servers" / "weather_server.py"
    assert server.is_file(), f"真 MCP server 脚本缺失：{server}"

    channel = SubprocessStdioChannel([sys.executable, str(server)])
    transport = StdioMCPTransport(channel, danger=DangerLevel.SAFE)
    registry = ToolRegistry()
    mgr = MCPManager(registry, transport)
    ctx = PortCtx(session_id="e2e-mcp-stdio", run_id="r-mcp")

    async def scenario() -> tuple[list[Any], list[Any], str, Any, dict[str, bool]]:
        try:
            injected = await mgr.connect("weather", ctx)
            listed = await mgr.list_tools("weather", ctx)
            value = await mgr.call_tool("weather", "get_weather", {"city": "深圳"}, ctx)
            via_registry = await registry.call(
                ToolCall(call_id="c-mcp", name="mcp__weather__get_weather",
                         args={"city": "深圳"}), ctx)
            health = await mgr.health(ctx)
            return injected, listed, value, via_registry, health
        finally:
            await transport.aclose()

    async def guarded() -> tuple[list[Any], list[Any], str, Any, dict[str, bool]]:
        return await asyncio.wait_for(scenario(), timeout=20)

    try:
        injected, listed, value, via_registry, health = run(guarded(), loop)
    except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
        pytest.skip(
            f"真 stdio MCP server 在此环境不可用：{type(exc).__name__}: {exc}")

    assert [s.name for s in injected] == ["mcp__weather__get_weather"]
    assert injected[0].source == "mcp" and injected[0].danger is DangerLevel.SAFE
    assert injected[0].parameters_schema["required"] == ["city"]
    assert [s.name for s in listed] == ["get_weather"]  # 门面列原始名，connect 才加前缀
    assert json.loads(value) == {
        "city": "深圳",
        "status": "offline",
        "message": "离线模式：不伪造天气数据，请设 DEEPSEEK_API_KEY 并关闭 CHAT_OFFLINE",
    }
    assert via_registry.ok is True and json.loads(via_registry.value) == json.loads(value)
    assert health == {"weather": True}


# ---------------------------------------------------------------------------
# 2. WebFetch 真本地 HTTP server（标准库 http.server + 框架真 HttpFetcher）
# ---------------------------------------------------------------------------
def test_infra_webfetch01_real_local_http_server(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=thread 里真起的 ThreadingHTTPServer(127.0.0.1:0) + GET /test-path 与 /tool-path；
    真实入口=HttpFetcher.fetch 真发 GET、WebFetchTool.fetch 经真 fetcher 再取一次；
    精确断言=server 端记录路径精确等于 ["/test-path","/tool-path"]、两次正文精确
    等于 server 固定响应、无 sk-、teardown 真关 server；
    失败判据=连不上/路径不符/正文不符/凭据泄漏均 FAIL，不用 MockTransport 冒充 HTTP。"""
    from harness.tools import WebFetchTool
    from ports.adapters.webfetch import HttpFetcher

    body = "真 HTTP 正文 e2e-webfetch-2026"
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            seen.append(self.path)
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: Any) -> None:
            return  # 真 server 静默，不打测试日志

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ctx = PortCtx(session_id="e2e-webfetch", run_id="r-web")
    try:
        async def scenario() -> tuple[str, str]:
            fetcher = HttpFetcher(timeout=5.0, max_bytes=100_000)
            try:
                direct = await fetcher.fetch(
                    f"http://127.0.0.1:{port}/test-path", ctx)
                tool = WebFetchTool(fetcher)
                via_tool = await tool.fetch(
                    f"http://127.0.0.1:{port}/tool-path", ctx)
                return direct, via_tool
            finally:
                await fetcher.aclose()

        direct, via_tool = run(scenario(), loop)
        assert_no_secret(direct)
        assert_no_secret(via_tool)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert seen == ["/test-path", "/tool-path"]
    assert direct == body and via_tool == body


# ---------------------------------------------------------------------------
# 3. stream_events 真路径（ModelGateway 真消费 SSE / RecordingLLM 包装）
# ---------------------------------------------------------------------------
def test_infra_stream01_real_sse_through_gateway(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=OpenAICompatLLM + httpx.MockTransport 提供的真 SSE 帧（两块正文+usage）；
    真实入口=ModelGateway.stream_events 逐事件消费 + 网关结算写缓存；
    精确断言=事件序列精确为 TEXT_DELTA('he')→TEXT_DELTA('llo')→DONE、
    POST body stream=True/stream_options.include_usage=True、缓存写回 'hello' 且
    usage=(2,2)；
    失败判据=事件序列或真实请求/缓存内容不符 FAIL。"""
    from compose import LLMRequest
    from harness.model import ModelGateway
    from ports.adapters.llm import OpenAICompatLLM

    sse = (
        'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n'
        'data: [DONE]\n\n'
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM(base_url="https://api.test/openai", api_key="k",
                          model="m", client=client)
    gateway = ModelGateway(llm=llm)
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    ctx = PortCtx(session_id="e2e-stream", run_id="r-stream")

    async def scenario() -> tuple[list[Any], Any]:
        try:
            events = [e async for e in gateway.stream_events(req, ctx)]
            cached = await gateway.cache.get(req, version="current")
            return events, cached
        finally:
            await client.aclose()

    events, cached = run(scenario(), loop)
    assert [(e.kind, e.data) for e in events if e.kind is not StreamEventKind.USAGE] == [
        (StreamEventKind.TEXT_DELTA, "he"),
        (StreamEventKind.TEXT_DELTA, "llo"),
        (StreamEventKind.DONE, None),
    ]
    # 修复回归：流末必须发 USAGE（此前 wrap_text_stream 快照 last_usage 为 None 导致缺失）
    usages = [e.data for e in events if e.kind is StreamEventKind.USAGE]
    assert len(usages) == 1 and usages[0].prompt_tokens == 2, \
        f"流末应有且仅有一个 USAGE 事件，实际 {usages}"
    assert posted and posted[0]["stream"] is True
    assert posted[0]["stream_options"] == {"include_usage": True}
    assert cached is not None and cached.content == "hello"
    assert cached.usage.prompt_tokens == 2 and cached.usage.completion_tokens == 2


def test_infra_stream02_recording_llm_wrapped(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=RecordingLLM(answer='AB') 真正逐块 stream（无 stream_events 能力）；
    真实入口=ModelGateway.stream_events 的 wrap_text_stream 兜底分支；
    精确断言=事件序列 TEXT_DELTA('A')→TEXT_DELTA('B')→DONE、stream_calls=1、
    last_usage=(2,2) 且缓存写回 'AB'；
    失败判据=事件/调用计数/缓存任一不符 FAIL。"""
    from compose import LLMRequest
    from harness.model import ModelGateway
    from impls.recording_llm import RecordingLLM

    llm = RecordingLLM(answer="AB")
    gateway = ModelGateway(llm=llm)
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    ctx = PortCtx(session_id="e2e-stream-rec", run_id="r-rec")

    async def scenario() -> tuple[list[Any], Any]:
        events = [e async for e in gateway.stream_events(req, ctx)]
        cached = await gateway.cache.get(req, version="current")
        return events, cached

    events, cached = run(scenario(), loop)
    assert [(e.kind, e.data) for e in events if e.kind is not StreamEventKind.USAGE] == [
        (StreamEventKind.TEXT_DELTA, "A"),
        (StreamEventKind.TEXT_DELTA, "B"),
        (StreamEventKind.DONE, None),
    ]
    usages = [e.data for e in events if e.kind is StreamEventKind.USAGE]
    assert len(usages) == 1 and usages[0].prompt_tokens == 2, \
        f"wrap_text_stream 兜底也必须在流末发 USAGE，实际 {usages}"
    assert llm.stream_calls == 1
    assert llm.last_usage is not None
    assert llm.last_usage.prompt_tokens == 2 and llm.last_usage.completion_tokens == 2
    assert cached is not None and cached.content == "AB"


# ---------------------------------------------------------------------------
# 4. checkpoint + 审计验链真路径（assemble_runtime 共享 EventStore）
# ---------------------------------------------------------------------------
def test_infra_checkpoint_and_audit_chain_real(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=assemble_runtime(store=InMemoryEventStore()) + 真注册的 LOW 工具 +
    rt.run 真跑一轮 tool→Terminate；
    真实入口=rt.checkpoint.save/load 真存真取、CheckpointManager.load→replay_snapshot
    真回放、replay_audit_events(R1AuditEventSink(shared)) 真回放审计、verify_audit_chain 真验链；
    精确断言=快照 state.output/last_step_kinds 一致、replay_snapshot 恢复 output 与步数、
    审计事件非空且 action=tool.low.call 且 data_touched 含工具名、验链为 True；
    失败判据=回放为空/验链 False/出现 NO_AUDIT/字段不符 FAIL。"""
    from api import assemble_runtime, verify_audit_chain
    from api.wiring import R1AuditEventSink, replay_audit_events
    from compose import ToolCall
    from harness.durability import replay_snapshot
    from harness.tools import clear_pending, tool
    from runtime import Invoke as RTInvoke, Terminate, checkpoint

    clear_pending()

    @tool(name="e2e_low_tool", description="e2e 审计验链工具", danger=DangerLevel.LOW)
    def _low() -> str:
        return "ok-low"

    shared = InMemoryEventStore()
    rt = assemble_runtime(store=shared)
    assert rt.checkpoint.inner is shared
    assert rt.tools.register_pending() == ["e2e_low_tool"]

    sid = "e2e-checkpoint-audit"

    def program():
        yield RTInvoke(port="tool", payload={
            "name": "e2e_low_tool", "args": {}, "call_id": "c-audit-1",
        })
        yield Terminate(output="真完成")

    async def scenario() -> tuple[Any, Any, Any, Any, tuple[Any, ...], bool]:
        session = Session(session_id=sid, program_factory=program)
        result = await rt.run(session)
        # 显式再存一次：证明 rt.checkpoint.save/load 真写真读共享 store。
        await rt.checkpoint.save(sid, checkpoint(session))
        loaded = await rt.checkpoint.load(sid)
        snap = rt.checkpoint.manager().load(sid)
        checkpointed = await snap if snap is not None else None
        final = replay_snapshot(checkpointed) if checkpointed is not None else None
        events = await replay_audit_events(R1AuditEventSink(shared), sid)
        return result, session, loaded, final, events, verify_audit_chain(rt.audit)

    result, session, loaded, final, events, chain_ok = run(scenario(), loop)

    assert result.output == "真完成"
    assert loaded is not None and loaded["session_id"] == sid
    assert loaded["state"]["output"] == "真完成"
    # last_step_kinds 记录的是最后一步的 Effect 序列（不是全轨迹）。
    assert loaded["state"]["last_step_kinds"] == ["terminate"]
    assert len(loaded["events"]) == len(session.events) == 2
    assert final is not None and final.output == "真完成"
    assert "terminate" in final.last_step_kinds and final.budgets.used_turns >= 1
    assert events, "审计事件链不可为空"
    assert events[0].action == "tool.low.call"
    assert "e2e_low_tool" in events[0].data_touched
    assert chain_ok is True
    # NO_AUDIT 是审计缺席时 registry 的失败码；真跑不应出现。
    assert all(
        str(getattr(r, "error", "")) != "NO_AUDIT"
        for ev in session.events for r in ev.results
    )


# ---------------------------------------------------------------------------
# 5. runtime 导出真用例（fold / replay_events / Message / Terminate / checkpoint / store）
# ---------------------------------------------------------------------------
def _infra_call(effect: Any, ctx: Any) -> Any:
    """真实 Port 执行体：LLM 回正文+usage，工具回信封，供 Runner 真跑。"""
    if isinstance(effect, Invoke) and effect.port == "llm":
        return _ok({"content": "answer", "tool_calls": []},
                   usage=Usage(prompt_tokens=3, completion_tokens=2, cost_usd=0.5))
    if isinstance(effect, Invoke) and effect.port == "tool":
        return _ok({"ok": True, "value": "x"})
    return _ok(None)


def _ok(data: Any, usage: Usage | None = None) -> Any:
    """构造 Runner/DTO 用的 Result（真框架类型，非本地假类）。"""
    from runtime import Result as R
    return R.success(data, usage=usage)


def _infra_program():
    """真实 Program：LLM → tool → Terminate，真产出三类 Effect。"""
    yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Invoke(port="tool", payload={
        "name": "echo", "args": {"v": "x"}, "call_id": "c1",
    })
    from runtime import Terminate
    yield Terminate(output="终态-X")


def test_infra_runtime01_fold_exact_state() -> None:
    """输入=真实 Invoke/Result/Terminate 序列（含 usage）；
    真实入口=runtime.fold 直接折叠；
    精确断言=assistant 消息正文、output、last_step_kinds=('invoke','terminate')、
    used_turns=2、used_cost_usd=0.25、last_tokens=3、last_reason='end'；
    失败判据=任一字段不符 FAIL。"""
    from runtime import Terminate, fold

    effects = [
        Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]}),
        Terminate(output="折叠完成"),
    ]
    results = [
        _ok({"content": "hi", "tool_calls": []},
            usage=Usage(prompt_tokens=2, completion_tokens=1, cost_usd=0.25)),
        _ok(None),
    ]
    state = fold(AgentState(), effects, results, reason="end")
    assert [m.role for m in state.messages] == ["assistant"]
    assert state.messages[0].content == "hi"
    assert state.output == "折叠完成"
    assert state.last_step_kinds == ("invoke", "terminate")
    assert state.budgets.used_turns == 2
    assert state.budgets.used_cost_usd == 0.25
    assert state.last_tokens == 3
    assert state.last_reason == "end"


def test_infra_runtime02_replay_events_from_real_run(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=Runner 真跑 _infra_program 产出的 session.events；
    真实入口=runtime.replay_events(events, AgentState())；
    精确断言=回放终态 output/budgets/last_step_kinds 与真执行终态逐项相等、
    事件数=3；
    失败判据=回放心智模型与真执行不一致 FAIL。"""
    from runtime import Runner, replay_events

    runner = Runner(_infra_call)
    session = Session(session_id="rt-infra-replay", program_factory=_infra_program)
    result = run(runner.run(session), loop)
    replayed = replay_events(session.events, AgentState())
    assert len(session.events) == 3
    assert replayed.output == result.state.output == "终态-X"
    assert replayed.budgets == result.state.budgets
    # last_step_kinds 只保留最后一步（Terminate），三步轨迹由 events 承载。
    assert replayed.last_step_kinds == result.state.last_step_kinds == ("terminate",)
    assert [e.payload["name"] for ev in session.events for e in ev.effects
            if isinstance(e, Invoke) and e.port == "tool"] == ["echo"]


def test_infra_runtime03_message_from_real_tool_result(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=真跑 LLM→tool→Terminate 后 state 中真实 Message；
    真实入口=fold 的 _message_for 生成消息 + Message.to_dict()；
    精确断言=role 序列 ['assistant','tool']、tool 消息 name='c1'、content 解析后
    精确 {ok,value,error}、to_dict() 四字段精确；
    失败判据=消息内容/字段不符 FAIL（不靠 isinstance 自证）。"""
    from runtime import Runner

    runner = Runner(_infra_call)
    session = Session(session_id="rt-infra-message", program_factory=_infra_program)
    run(runner.run(session), loop)
    messages = session.state.messages
    assert [m.role for m in messages] == ["assistant", "tool"]
    tool_msg = messages[1]
    assert tool_msg.name == "c1"
    assert json.loads(tool_msg.content) == {"ok": True, "value": "x", "error": None}
    assert tool_msg.to_dict() == {
        "role": "tool",
        "content": tool_msg.content,
        "name": "c1",
        "tool_calls": [],
    }


def test_infra_runtime04_terminate_drives_output(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=只 yield Terminate(output='只有终止') 的真 Program；
    真实入口=Runner.run 真执行；
    精确断言=RunResult.output 与 Session.state.output 都为 '只有终止'、
    last_step_kinds=('terminate',)、事件数=1；
    失败判据=Terminate 未驱动终态 FAIL。"""
    from runtime import Runner, Terminate

    def program():
        yield Terminate(output="只有终止")

    runner = Runner(_infra_call)
    session = Session(session_id="rt-infra-terminate", program_factory=program)
    result = run(runner.run(session), loop)
    assert result.output == "只有终止"
    assert session.state.output == "只有终止"
    assert session.state.last_step_kinds == ("terminate",)
    assert len(session.events) == 1


def test_infra_runtime05_checkpoint_restore_from_real_run(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=真跑 _infra_program 后的 Session；
    真实入口=runtime.checkpoint 产快照 + runtime.restore_checkpoint 重建 Session；
    精确断言=session_id/output/事件数一致、fingerprint=fingerprint(events)、
    最后一条 Event 的 effects[0].output='终态-X'（真实 DTO 复活，非 plain dict）；
    失败判据=重建状态或事件形态不符 FAIL。"""
    from runtime import Runner, checkpoint, fingerprint, restore_checkpoint

    runner = Runner(_infra_call)
    session = Session(session_id="rt-infra-cp", program_factory=_infra_program)
    run(runner.run(session), loop)
    snap = checkpoint(session)
    assert snap["session_id"] == "rt-infra-cp"
    assert snap["fingerprint"] == fingerprint(session.events)
    restored = restore_checkpoint(snap, _infra_program)
    assert restored.session_id == "rt-infra-cp"
    assert restored.state.output == "终态-X"
    assert len(restored.events) == len(session.events) == 3
    assert restored.events[-1].effects[0].output == "终态-X"


def test_infra_runtime06_event_store_roundtrip_real(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=Runner(store=InMemoryEventStore) 真跑后的 checkpoint(session) 快照；
    真实入口=InMemoryEventStore.save/load 真存真取；
    精确断言=load 快照 session_id/fingerprint/事件数与真快照逐项一致；
    失败判据=存储往返丢失字段 FAIL。"""
    from runtime import Runner, checkpoint

    store = InMemoryEventStore()
    runner = Runner(_infra_call, store=store)
    session = Session(session_id="rt-infra-store", program_factory=_infra_program)
    run(runner.run(session), loop)
    snap = checkpoint(session)

    async def scenario() -> Any:
        await store.save("rt-infra-store-manual", snap)
        return await store.load("rt-infra-store-manual")

    loaded = run(scenario(), loop)
    assert loaded is not None
    assert loaded["session_id"] == snap["session_id"] == "rt-infra-store"
    assert loaded["fingerprint"] == snap["fingerprint"]
    assert len(loaded["events"]) == len(snap["events"]) == 3
