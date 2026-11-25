"""补齐最后一批真实 e2e：剩余零执行函数里的「用户可观察行为分支」。

纪律（与 tests/e2e 其余文件一致）：
- 真入口 + 精确断言；不用 isinstance / hasattr / callable / or True / 恒真式当证据；
- 每个用例 docstring 写清 输入 → 真实入口 → 精确断言 → 失败判据；
- 每用例独立实例；异步统一走 conftest 的 loop + run；
- 不改 src/，不碰其他文件。

覆盖目标（e2e 具名函数覆盖率统计中零执行的具名函数）：
CLI._eval / _replay / _gate；ToolUse.compile.empty / Handoff.compile.empty /
Graph.compile.invalid；ModelGateway._resolution_error_response /
_raise_resolution_error；SimilarityCache._identity；HttpMCPTransport._parse_sse；
Replayed.__getattr__。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from api import CLI
from archs import Graph, Handoff, ToolUse
from compose import DangerLevel, LLMRequest, LLMResponse, PortCtx, Usage
from conftest import run
from harness.eval.types import EvalCase
from harness.model.cache import SimilarityCache
from harness.model.gateway import ModelGateway
from harness.model.registry import InMemoryModelRegistry
from ports.adapters.mcp import HttpMCPTransport
from runtime import (
    AgentState,
    Invoke,
    Result,
    Session,
    Terminate,
    checkpoint,
    replay_events,
    restore_checkpoint,
)
from tests.r12.conftest import drive


# --------------------------------------------------------------------------- #
# 本地夹具（每用例独立实例，不跨用例共享状态）
# --------------------------------------------------------------------------- #
class _EchoLLM:
    """履约 LLMPort + LLMPortWithUsage：complete 真回确定性答复，调用计数可见。"""

    name = "echo_llm"

    def __init__(self, answer: str = "OK") -> None:
        self.answer = answer
        self.complete_calls = 0
        self.last_usage: Usage | None = None

    async def complete(self, request: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.complete_calls += 1
        usage = Usage(prompt_tokens=1, completion_tokens=len(self.answer))
        self.last_usage = usage
        return LLMResponse(content=self.answer, tool_calls=[], usage=usage, model=self.name)

    def stream(self, request: LLMRequest, ctx: PortCtx) -> Any:
        """解析失败场景不会走到流后端；仍提供契约形态以免误判为装配错误。"""
        raise AssertionError("stream backend must not be invoked on unresolved target")


class _TopicEmbedder:
    """自定义 embedder：同话题文本映射同一向量（余弦 1），异话题正交（余弦 0）。"""

    name = "topic_embedder"

    async def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "hello" in text else (0.0, 1.0)


def _req(text: str) -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}])


def _event(effects: tuple[Any, ...], results: tuple[Any, ...], reason: str = "") -> Any:
    """最小具名 Event：replay_events 只读 effects/results/reason 三属性。"""
    class _Ev:
        pass

    ev = _Ev()
    ev.effects = effects
    ev.results = results
    ev.reason = reason
    return ev


# =========================================================================== #
# 1. CLI._eval / _replay / _gate：真入口 CLI().run
# =========================================================================== #
def test_cli_eval_single_case_exact_score() -> None:
    """输入=case_id/expected/output 单用例；入口=CLI().run("eval",...)（内部真委托 R10
    RuleEval.score）；断言=CLIResult.ok 为 True、data 精确等于 {"case": id, "score": 1.0/0.0}；
    失败判据=score 不是精确 1.0/0.0、ok 不为 True 或 data 多键缺键均 FAIL。"""
    hit = CLI().run("eval", case_id="c1", expected="a", output="a")
    assert hit.ok is True
    assert hit.data == {"case": "c1", "score": 1.0}

    miss = CLI().run("eval", case_id="c2", expected="a", output="b")
    assert miss.ok is True
    assert miss.data == {"case": "c2", "score": 0.0}


def test_cli_eval_suite_threshold_gate() -> None:
    """输入=suite 名 + cases(EvalCase 带实际输出 actual) + threshold；入口=CLI().run("eval")
    的 CI 套件分支（真委托 RuleEval.regression + 阈值判定）；断言=达标时 ok True 且
    data 精确 {suite,total,passed,average,threshold}，不达标时 ok False 且 average 精确；
    失败判据=阈值门未生效（全 0 分仍 ok=True）或统计字段不符 FAIL。"""
    passing = CLI().run(
        "eval", suite="ci-pass", threshold=0.85,
        cases={"c1": EvalCase(case_id="c1", input="", expected="ok", actual="ok")},
    )
    assert passing.ok is True
    assert passing.data == {
        "suite": "ci-pass", "total": 1, "passed": 1,
        "average": 1.0, "threshold": 0.85,
    }

    blocked = CLI().run(
        "eval", suite="ci-block", threshold=0.9,
        cases={"c1": EvalCase(case_id="c1", input="", expected="ok", actual="bad")},
    )
    assert blocked.ok is False
    assert blocked.data == {
        "suite": "ci-block", "total": 1, "passed": 0,
        "average": 0.0, "threshold": 0.9,
    }


def test_cli_replay_events_terminal_output() -> None:
    """输入=session_id + 一条携带 Terminate(output) 的事件；入口=CLI().run("replay")
    （真委托 runtime.replay_events，不重造 fold）；断言=ok True、data 精确
    {"session": id, "output": 终态值}；失败判据=回放 output 不是事件终态值 FAIL。"""
    ev = _event(
        (Terminate(output="replay-final"),),
        (Result.success(None),),
        reason="",
    )
    res = CLI().run("replay", session_id="s-e2e", events=(ev,))
    assert res.ok is True
    assert res.data == {"session": "s-e2e", "output": "replay-final"}


def test_cli_gate_module_status() -> None:
    """输入=module 名；入口=CLI().run("gate")；断言=ok True 且 data 精确
    {"module": 名}；失败判据=模块名丢失或被改写 FAIL。"""
    res = CLI().run("gate", module="r11")
    assert res.ok is True
    assert res.data == {"module": "r11"}


# =========================================================================== #
# 2. recipes 三个空/无效分支：tests.r12.conftest.drive 真驱动 Program
# =========================================================================== #
def test_tooluse_compile_empty_effect_sequence() -> None:
    """输入=ToolUse([]) 空工具目录；入口=ToolUse.compile() 产出的 Program 经 drive 真驱动；
    断言=唯一 Effect 精确为 Terminate(output=[], reason="tool_all_failed")，终态 output 为 []；
    失败判据=空目录仍发起工具调用或 reason/output 不符 FAIL。"""
    effects, state = drive(lambda: ToolUse([]).compile())
    assert len(effects) == 1
    assert effects[0] == Terminate(output=[], reason="tool_all_failed")
    assert state.output == []


def test_handoff_compile_empty_effect_sequence() -> None:
    """输入=Handoff({}) 空角色表；入口=Handoff.compile() 经 drive 真驱动；
    断言=唯一 Effect 精确为 Terminate(output=None, reason="no_route")，终态 output 为 None；
    失败判据=无角色仍路由到后端或 reason 不符 FAIL。"""
    effects, state = drive(lambda: Handoff({}).compile())
    assert len(effects) == 1
    assert effects[0] == Terminate(output=None, reason="no_route")
    assert state.output is None


def test_graph_compile_invalid_effect_sequence() -> None:
    """输入=Graph() 无节点无出口的无效图；入口=Graph.compile() 经 drive 真驱动；
    断言=唯一 Effect 精确为 Terminate(output=None, reason="graph_invalid")，终态 output None
    （fail-closed，零后端 Effect）；失败判据=无效图产出 Invoke 或 reason 不符 FAIL。"""
    effects, state = drive(lambda: Graph().compile())
    assert len(effects) == 1
    assert effects[0] == Terminate(output=None, reason="graph_invalid")
    assert state.output is None


# =========================================================================== #
# 3. ModelGateway 解析失败三态：complete 折叠错误 / stream 抛稳定码
# =========================================================================== #
def test_gateway_complete_unregistered_target_error_response(loop: asyncio.AbstractEventLoop) -> None:
    """输入=degrade_target 指向注册表未登记的 ghost 模型；入口=gateway.complete(...)
    经 _resolve_llm 得 TargetUnresolved 后走 _resolution_error_response；
    断言=返回 LLMResponse.error 精确 "TARGET_UNRESOLVED: ghost"、content 空、后端调用计数为 0；
    失败判据=裸异常穿透、静默回退默认后端或错误码不符 FAIL。"""
    llm = _EchoLLM()
    gateway = ModelGateway(llm=llm, registry=InMemoryModelRegistry())
    response = run(
        gateway.complete(_req("hi"), PortCtx(session_id="s-e2e-unreg"), degrade_target="ghost"),
        loop,
    )
    assert response.error == "TARGET_UNRESOLVED: ghost"
    assert response.content == ""
    assert llm.complete_calls == 0


def test_gateway_complete_refuse_error_response(loop: asyncio.AbstractEventLoop) -> None:
    """输入=degrade_target="refuse"（拒答降级）；入口=gateway.complete(...) 得
    RefuseExecution 后走 _resolution_error_response；断言=error 精确
    "REFUSED: degraded to refusal; no backend invoked"、content 空、后端调用计数 0；
    失败判据=拒答仍调后端或未带 REFUSED 稳定码 FAIL。"""
    llm = _EchoLLM()
    gateway = ModelGateway(llm=llm, registry=InMemoryModelRegistry())
    response = run(
        gateway.complete(_req("hi"), PortCtx(session_id="s-e2e-refuse"), degrade_target="refuse"),
        loop,
    )
    assert response.error == "REFUSED: degraded to refusal; no backend invoked"
    assert response.content == ""
    assert llm.complete_calls == 0


def test_gateway_stream_unresolved_target_raises_stable_code(loop: asyncio.AbstractEventLoop) -> None:
    """输入=degrade_target 指向未登记模型；入口=gateway.stream(...) 经 _raise_resolution_error；
    断言=ValueError 消息精确以 "TARGET_UNRESOLVED: ghost-stream" 打头、零文本块产出、
    后端零调用；失败判据=裸 ValueError 无稳定码、静默回退或吐出内容 FAIL。"""
    llm = _EchoLLM()
    gateway = ModelGateway(llm=llm, registry=InMemoryModelRegistry())

    async def scenario() -> list[str]:
        chunks: list[str] = []
        async for chunk in gateway.stream(
            _req("hi"), PortCtx(session_id="s-e2e-stream"), degrade_target="ghost-stream"
        ):
            chunks.append(chunk)
        return chunks

    with pytest.raises(ValueError) as caught:
        run(scenario(), loop)
    assert str(caught.value) == "TARGET_UNRESOLVED: ghost-stream"
    assert llm.complete_calls == 0


# =========================================================================== #
# 4. SimilarityCache：自定义 embedder 真命中/未命中 + _identity 精确回显
# =========================================================================== #
def test_similarity_cache_custom_embedder_hit_miss_and_identity(
    loop: asyncio.AbstractEventLoop,
) -> None:
    """输入=注入 _TopicEmbedder 的 SimilarityCache，先 put "hello world" 响应，再 get
    "hello there"（同向量）与 "goodbye world"（正交向量）；入口=cache.put/get 真走余弦
    相似度路径，并直调纯私有 helper _identity；断言=同话题命中返回原响应、异话题未命中
    None、hits/misses 计数精确为 1/1、_identity("raw text") 精确回显原文；
    失败判据=同义未命中、异义误命中、计数错或 _identity 改写原文 FAIL。
    注：_identity 在 cache.py 中是未被 put/get 调用的死 helper（见源码 397~399），
    公开路径无法触达其函数体，故以真实例直调并按精确返回值断言。"""
    cache = SimilarityCache(threshold=0.85, embedder=_TopicEmbedder())
    first = LLMResponse(content="cached-answer", tool_calls=[], usage=Usage(), model="echo")

    async def scenario() -> tuple[Any, Any]:
        await cache.put(_req("hello world"), first)
        hit = await cache.get(_req("hello there"))
        miss = await cache.get(_req("goodbye world"))
        return hit, miss

    hit, miss = run(scenario(), loop)
    assert hit.content == "cached-answer"
    assert miss is None
    assert (cache.hits, cache.misses) == (1, 1)
    assert cache._identity("raw text") == "raw text"


# =========================================================================== #
# 5. HttpMCPTransport._parse_sse：MockTransport 提供真 SSE 帧
# =========================================================================== #
def test_http_mcp_sse_frames_list_and_call_tools(loop: asyncio.AbstractEventLoop) -> None:
    """输入=httpx.MockTransport 按 JSON-RPC method 返回 SSE 帧（含非 data: 干扰行），
    走真握手 initialize → notifications/initialized → tools/list → tools/call；
    入口=HttpMCPTransport.list_tools/call_tool（内部 _post → _parse_sse 真解析）；
    断言=工具名 ["remote"]、parameters_schema 精确、call_tool 文本精确 "SSE-OK"、
    捕获 method 序列精确；失败判据=SSE 帧解析不出工具名/结果或握手序列不符 FAIL。"""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        method = frame["method"]
        captured.append(method)
        if "id" not in frame:
            return httpx.Response(202, text="")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [{
                "name": "remote", "description": "d",
                "inputSchema": {"type": "object", "properties": {}},
            }]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "SSE-OK"}]}
        else:
            raise AssertionError(f"unexpected method: {method}")
        payload = {"jsonrpc": "2.0", "id": frame["id"], "result": result}
        event = f": keep-alive\nevent: message\ndata: {json.dumps(payload)}\n\n"
        return httpx.Response(200, text=event, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpMCPTransport("https://mcp.example/sse", client=client, danger=DangerLevel.SAFE)

    async def scenario() -> tuple[list[Any], Any]:
        try:
            specs = await transport.list_tools("srv")
            value = await transport.call_tool("srv", "remote", {"x": 1})
            return specs, value
        finally:
            await client.aclose()

    specs, value = run(scenario(), loop)
    assert [s.name for s in specs] == ["remote"]
    assert specs[0].parameters_schema == {"type": "object", "properties": {}}
    assert specs[0].source == "mcp"
    assert value == "SSE-OK"
    assert captured == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


def test_http_mcp_sse_without_data_frame_fails_closed(loop: asyncio.AbstractEventLoop) -> None:
    """输入=MockTransport 回 content-type=text/event-stream 但正文只有注释/event 行（无 data:）；
    入口=HttpMCPTransport.list_tools 首次握手即走 _parse_sse；
    断言=RuntimeError 消息精确 "MCP SSE has no data frame"；
    失败判据=无 data 帧却返回空响应或裸 JSON 解析异常 FAIL。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=": keep-alive\nevent: message\n\n",
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpMCPTransport("https://mcp.example/sse-bad", client=client)

    async def scenario() -> Any:
        try:
            return await transport.list_tools("srv")
        finally:
            await client.aclose()

    with pytest.raises(RuntimeError) as caught:
        run(scenario(), loop)
    assert str(caught.value) == "MCP SSE has no data frame"


# =========================================================================== #
# 6. Replayed.__getattr__：checkpoint → restore → replay_events 真产出的代理对象
# =========================================================================== #
def _replay_program() -> Any:
    """真实 Program：一次 LLM Invoke（回嵌套 dict）+ Terminate。"""
    yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
    yield Terminate(output={"visible": "done"})


def _replay_call(effect: Any, ctx: Any) -> Any:
    """真实 Port 执行体：LLM 返回带自定义嵌套键的 dict（快照恢复后复活为 Replayed）。"""
    if effect.port == "llm":
        return Result.success({"custom_field": {"nested": 7}, "content": "answer"})
    return Result.success(None)


def test_replayed_getattr_from_checkpoint_replay(loop: asyncio.AbstractEventLoop) -> None:
    """输入=Runner 真跑 _replay_program 得 Session → checkpoint 快照 →
    restore_checkpoint 复活（revive 把结果 dict 变成 Replayed）→ replay_events 真回放；
    入口=复活事件结果上的属性访问 obj.custom_field.nested 与缺失键访问；
    断言=回放终态 output 精确 {"visible": "done"}、嵌套自定义属性精确为 7、
    缺失属性抛 AttributeError 且消息精确为键名；
    失败判据=属性访问退化为普通 dict 报错、键值错或回放 output 不符 FAIL。"""
    from runtime import Runner

    session = Session(session_id="s-e2e-replayed", program_factory=_replay_program)
    run(Runner(_replay_call).run(session), loop)
    snapshot = checkpoint(session)
    restored = restore_checkpoint(snapshot, _replay_program)

    final = replay_events(restored.events, restored.state)
    assert final.output == {"visible": "done"}

    revived = restored.events[0].results[0].data
    assert revived.custom_field.nested == 7
    with pytest.raises(AttributeError) as caught:
        _ = revived.missing_key
    assert str(caught.value) == "missing_key"
