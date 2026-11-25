"""双实现矩阵真实 e2e：Port / Capability / Tool / Strategy 的「内置 + 用户自定义」双路径。

纪律（用户铁律）：
- 证据只认「真调用 + 可观察副作用」：调用计数 / HTTP 接收端实收 request /
  span 登记记录 / LLM 实收 messages；不用 isinstance / registry.has / hasattr /
  手搓 DTO 自证；断言必须可鉴别（失败时能红）。
- 每个用例独立实例、独立 loop（conftest 的 loop + run），不跨用例共享。
- src/ 零改：所有替身与 Arch 都定义在用户侧测试文件。

用例分组：
- PORT-01~14：PortRegistry.register/resolve/replace 真生效（identity + 真调协议方法 +
  计数）；重点 llm/tool/telemetry/memory/event_stream/mcp 另走 Agent.asay 真派发。
- CAP-01~11(+12)：Capability.install→Registry.resolve 注入实例 identity + 真消费计数；
  不注入时默认实例能工作。
- TOOL-01~03：@tool schema 真进 catalog/schema_of；call 真执行有副作用；
  register_tool 自定义工具被 Agent.asay 真调用。
- STRAT-01~03：自定义 Intent/Reflect 策略经 Capability 真调用；MajorityVote 精确多数。
"""
from __future__ import annotations

import json
from string import Template
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from api import Agent, PortRegistry, PROTOCOL_NAMES, STREAM_TOPIC
from archs.base import MajorityVote, StaticCheckReport
from compose import (
    DangerLevel, DisclosurePacket, LLMRequest, LLMResponse, PortCtx,
    Registry, SkillManifest, ToolResult, ToolSpec, Usage, current_input, make_call,
)
from compose.context import Invocation
from harness.cognition import Critique, IntentResult
from harness.cognition.capability import CognitionCapability
from harness.cognition.reflection_capability import ReflectionCapability
from harness.context import ContextMemoryManager, MemoryCapability
from harness.eval.capability import EvalCapability
from harness.eval.rules import RuleEval
from harness.memory import MemoryStore
from harness.memory.store import InMemoryStorePort
from harness.model import InMemoryModelRegistry, ModelCapability
from harness.observability import EventEnvelope, InMemoryEventStream, NoopTelemetry
from harness.observability.capability import ObservabilityCapability
from harness.prompt import PromptCapability, PromptTemplate
from harness.rag import (
    DefaultPostProcessor, Document, HashEmbedder, InMemoryVectorStore,
    RAGPipeline, RecursiveSplitter, VectorRetriever,
)
from harness.rag.capability import RAGCapability
from harness.resilience.capability import ResilienceCapability
from harness.security import (
    AuditLogger, ConsoleHumanPort, LocalSandbox, SecurityCapability,
)
from harness.skills.provider import FileSkillProvider
from harness.tools import ToolRegistry, ToolsCapability, clear_pending, tool
from harness.tools.index import ToolIndex
from harness.tools.mcp import MCPManager
from ports.adapters.llm import OpenAICompatLLM
from ports.adapters.provider import LLMProviderCapability
from runtime import Invoke, Result, Terminate

from conftest import run
from impls.custom_telemetry import CustomTelemetry
from impls.recording_llm import RecordingLLM
from impls.user_memory import UserMemory


# --------------------------------------------------------------------------- #
# 用户侧最小自定义实现（真实履约 + 调用计数，src 零改）
# --------------------------------------------------------------------------- #
class SpyLLM:
    name = "spy_llm"

    def __init__(self, answer: str = "SPY-LLM") -> None:
        self.answer = answer
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []
        self.last_usage: Usage | None = None

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls += 1
        messages = [dict(m) for m in req.messages]
        self.seen.append(messages)
        usage = Usage(prompt_tokens=len(messages), completion_tokens=len(self.answer))
        self.last_usage = usage
        return LLMResponse(content=self.answer, usage=usage, model="spy")

    async def stream(self, req: LLMRequest, ctx: PortCtx):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.seen.append([dict(m) for m in req.messages])
        yield self.answer


class SpyToolPort:
    name = "spy_tool"

    def __init__(self) -> None:
        self.register_calls = 0
        self.catalog_calls = 0
        self.call_calls = 0
        self.specs: list[ToolSpec] = []
        self.effects: list[str] = []
        self.last_call: Any = None

    def register(self, spec: ToolSpec, fn: Any) -> None:
        self.register_calls += 1
        self.specs.append(spec)

    def catalog(self, level: int = 1) -> list[ToolSpec]:
        self.catalog_calls += 1
        return list(self.specs)

    async def call(self, call: Any, ctx: PortCtx) -> ToolResult:
        self.call_calls += 1
        self.last_call = call
        self.effects.append(f"called:{call.name}")
        return ToolResult(call_id=call.call_id, ok=True, value="SPY-TOOL-OK")


class SpySkillPort:
    name = "spy_skill"

    def __init__(self) -> None:
        self.discover_calls = 0
        self.catalog_calls = 0
        self.body_calls = 0

    def discover(self) -> list[SkillManifest]:
        self.discover_calls += 1
        return [SkillManifest(name="spy-skill", version="1.0",
                              description="用户技能", triggers=("触发",))]

    def catalog(self) -> list[SkillManifest]:
        self.catalog_calls += 1
        return self.discover()

    def body(self, name: str) -> str:
        self.body_calls += 1
        return f"SPY-BODY:{name}"


class SpyRAG:
    name = "spy_rag"

    def __init__(self, hits: list[Any] | None = None) -> None:
        self.ingest_calls = 0
        self.retrieve_calls = 0
        self.hits = list(hits) if hits is not None else ["SPY-RAG-HIT"]
        self.last_query = ""

    async def ingest(self, documents: list[Any], ctx: PortCtx) -> int:
        self.ingest_calls += 1
        return len(documents)

    async def retrieve(self, query: str, ctx: PortCtx) -> list[Any]:
        self.retrieve_calls += 1
        self.last_query = query
        return list(self.hits)


class SpyMemory:
    name = "spy_memory"

    def __init__(self) -> None:
        self.read_calls = 0
        self.write_calls = 0
        self.forget_calls = 0
        self.kv: dict[tuple[str, str], Any] = {}

    async def read(self, namespace: str, key: str, ctx: PortCtx) -> Any:
        self.read_calls += 1
        return self.kv.get((namespace, key))

    async def write(self, namespace: str, key: str, value: Any, ctx: PortCtx) -> None:
        self.write_calls += 1
        self.kv[(namespace, key)] = value

    async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:
        self.forget_calls += 1
        self.kv.pop((namespace, key), None)


class SpyStore:
    name = "spy_store"

    def __init__(self) -> None:
        self.save_calls = 0
        self.load_calls = 0
        self.data: dict[str, dict[str, Any]] = {}

    async def save(self, session_id: str, snapshot: dict[str, Any]) -> None:
        self.save_calls += 1
        self.data[session_id] = dict(snapshot)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        self.load_calls += 1
        return dict(self.data.get(session_id) or {})


class SpySandbox:
    name = "spy_sandbox"

    def __init__(self) -> None:
        self.run_calls = 0

    async def run(self, fn: Any, args: Any, ctx: PortCtx) -> Any:
        self.run_calls += 1
        return fn(**(dict(args) if isinstance(args, dict) else {}))


class CountingAudit:
    """用户自定义审计：包装真实 AuditLogger（验链有效）+ 计数。"""
    name = "counting_audit"

    def __init__(self) -> None:
        self.inner = AuditLogger()
        self.record_calls = 0

    async def record(self, event: dict[str, Any], ctx: PortCtx) -> None:
        self.record_calls += 1
        await self.inner.record(event, ctx)

    def events(self) -> tuple[Any, ...]:
        return self.inner.events()

    def bind_sink(self, sink: Any) -> None:
        self.inner.bind_sink(sink)


class SpyEval:
    name = "spy_eval"

    def __init__(self, value: float = 0.5) -> None:
        self.value = value
        self.score_calls = 0

    async def score(self, case_id: str, output: Any, ctx: PortCtx) -> float:
        self.score_calls += 1
        _ = (case_id, output)
        return self.value


class SpyHuman:
    name = "spy_human"

    def __init__(self, answer: str = "approve") -> None:
        self.answer = answer
        self.ask_calls = 0

    async def ask(self, question: str, options: list[str] | None,
                  ctx: PortCtx) -> str:
        self.ask_calls += 1
        return self.answer


class SpyEventStream:
    name = "spy_event_stream"

    def __init__(self) -> None:
        self.publish_calls = 0
        self.subscribe_calls = 0
        self.topics_calls = 0
        self.events: list[Any] = []

    def publish(self, event: EventEnvelope) -> None:
        self.publish_calls += 1
        self.events.append(event)

    def subscribe(self, topic: str, since: int | None = None):  # type: ignore[no-untyped-def]
        self.subscribe_calls += 1
        return self._iterate(topic, since)

    async def _iterate(self, topic: str, since: int | None):  # type: ignore[no-untyped-def]
        for event in list(self.events):
            if event.topic == topic and (since is None or event.seq > since):
                yield event

    def topics(self) -> list[str]:
        self.topics_calls += 1
        return sorted({e.topic for e in self.events})


class SpyDisclosure:
    name = "spy_disclosure"

    def __init__(self) -> None:
        self.disclose_calls = 0
        self.last_names: list[str] = []

    def disclose(self, names: list[str], skills: list[SkillManifest]) -> DisclosurePacket:
        self.disclose_calls += 1
        self.last_names = list(names)
        spec = ToolSpec(name="spy-tool", description="用户披露工具",
                        parameters_schema={}, danger=DangerLevel.SAFE)
        return DisclosurePacket(l1=(spec,), l2=(), l3={}, token_cost=7)


class SpyMCPTransport:
    name = "spy_mcp_server"

    def __init__(self) -> None:
        self.list_calls = 0
        self.call_calls = 0
        self.ping_calls = 0

    async def list_tools(self, server: str) -> list[ToolSpec]:
        self.list_calls += 1
        return [ToolSpec(
            name="echo", description="回显",
            parameters_schema={"type": "object",
                               "properties": {"text": {"type": "string"}},
                               "required": ["text"]},
            danger=DangerLevel.SAFE)]

    async def call_tool(self, server: str, tool_name: str,
                        args: dict[str, Any]) -> Any:
        self.call_calls += 1
        return f"MCP-ECHO:{args.get('text', '')}"

    async def ping(self, server: str) -> bool:
        self.ping_calls += 1
        return True


class CountingIntent:
    name = "counting_intent"

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0
        self.last_text = ""

    async def recognize(self, text: str, ctx: Any) -> IntentResult:
        self.calls += 1
        self.last_text = text
        return IntentResult(label=self.label, confidence=0.99,
                            gaps=("gap",), trace=("custom",))


class CountingReflector:
    name = "counting_reflector"

    def __init__(self, score: float = 0.42) -> None:
        self.score = score
        self.calls = 0
        self.args: tuple[Any, ...] = ()

    def critique(self, draft: str, goal: str, ctx: Any = None) -> Critique:
        self.calls += 1
        self.args = (draft, goal, ctx)
        return Critique(passed=False, score=self.score, comments="custom-critique")


class CountingEvaluator:
    name = "counting_evaluator"

    def __init__(self, value: float = 0.75) -> None:
        self.value = value
        self.calls = 0
        self.args: tuple[Any, ...] = ()

    async def score(self, case_id: str, output: Any, ctx: PortCtx) -> float:
        self.calls += 1
        self.args = (case_id, output, ctx)
        return self.value


class CountingPlugin:
    name = "counting_plugin"

    def __init__(self) -> None:
        self.transform_calls = 0
        self.invoke_calls = 0

    def __call__(self, core: Any) -> Any:
        self.transform_calls += 1

        async def wrapped(inv: Any) -> Any:
            self.invoke_calls += 1
            return await core(inv)

        return wrapped


class CountingJoin:
    name = "reduce"  # Supervisor.static_check 认可的 join 名

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def join(self, results: list[Any]) -> Any:
        self.calls += 1
        _ = results
        return self.answer


class AddArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int


class MulArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int
    y: float
    label: str


class JoinArch:
    """把 join 策略作为架构注入点真消费（同 Supervisor 的注入方式）。"""
    name = "e2e_join_arch"
    version = "1.0.0"

    def __init__(self, join: Any) -> None:
        self.join = join

    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(errors=(), arch=self.name, version=self.version,
                                 ports_required=(), budgets_resolved={})

    def compile(self) -> Any:
        join = self.join

        def program():
            text = current_input()
            out = join.join(["x", text, "x"])
            yield Terminate(output={"content": str(out)})
            return out

        return program()


# --------------------------------------------------------------------------- #
# 通用最小 Arch + 步骤构造（每用例独立实例）
# --------------------------------------------------------------------------- #
def _final(out: Any) -> Any:
    if isinstance(out, dict) and any(
            k in out for k in ("content", "text", "answer", "output")):
        return out
    if isinstance(out, str):
        return out
    return {"content": str(out)}


class PortArch:
    """按步骤真发 Invoke，末步 Terminate；不预设任何默认实现。"""
    name = "e2e_port_arch"
    version = "1.0.0"

    def __init__(self, steps: list[tuple[str, Any]]) -> None:
        self.steps = steps

    def static_check(self) -> StaticCheckReport:
        return StaticCheckReport(
            errors=(), arch=self.name, version=self.version,
            ports_required=tuple(p for p, _ in self.steps), budgets_resolved={})

    def compile(self) -> Any:
        steps = self.steps

        def program():
            text = current_input()
            last: Any = None
            for port, build in steps:
                last = yield Invoke(port=port, payload=build(text))
            yield Terminate(output=_final(last))
            return last

        return program()


def llm_step() -> tuple[str, Any]:
    return ("llm", lambda t: {"layer": "model",
                              "messages": [{"role": "user", "content": t}]})


def tool_step(name: str, args_fn: Any, danger: str = "SAFE") -> tuple[str, Any]:
    return ("tool", lambda t: {"name": name, "args": dict(args_fn(t)),
                               "danger": danger, "call_id": f"e2e-{name}"})


def port_step(port: str, build: Any) -> tuple[str, Any]:
    return (port, build)


def _http_llm(answer: str, seen: dict[str, Any]) -> OpenAICompatLLM:
    """框架内置 LLM 适配器 + httpx MockTransport 真接收端（不联网）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": answer}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            "model": "mock"})

    return OpenAICompatLLM(
        base_url="https://e2e-dual.test/v1", api_key="k-not-secret", model="mock",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _builtin_rag() -> RAGPipeline:
    store = InMemoryVectorStore()
    return RAGPipeline(
        splitter=RecursiveSplitter(chunk_size=40, overlap=8),
        embedder=HashEmbedder(dim=16), store=store,
        retriever=VectorRetriever(store), post=DefaultPostProcessor())


@pytest.fixture(autouse=True)
def _pending_tool_isolation():  # type: ignore[no-untyped-def]
    """@tool 全局待注册容器按用例隔离，避免跨用例污染计数。"""
    clear_pending()
    yield
    clear_pending()


# =========================================================================== #
# PORT-01~14
# =========================================================================== #
def test_port01_llm_dual(loop: Any) -> None:
    """PORT-01 llm 双路径。

    输入：自定义 SpyLLM ×2；框架内置 OpenAICompatLLM（MockTransport HTTP 接收端）。
    真实入口：PortRegistry.register/resolve/replace；SpyLLM.complete；
    OpenAICompatLLM.complete（HTTP request 实收）；Agent.asay→ModelCapability→llm port。
    精确断言：resolve is 注入实例；自定义 complete 计数与实收 messages 原文精确；
    replace 后旧实例计数冻结；HTTP 接收端 URL/body 精确；asay 答复精确且自定义被真调。
    失败判据：identity 不符 / 计数 0 / HTTP 接收端为空 / LLM 实收 messages 不含原文即红。
    """
    async def scenario() -> None:
        assert "llm" in PROTOCOL_NAMES()
        reg = PortRegistry()
        spy1 = SpyLLM("PORT01-SPY1")
        reg.register("llm", spy1)
        assert reg.resolve("llm") is spy1
        r1 = await spy1.complete(
            LLMRequest(messages=[{"role": "user", "content": "PORT01-Q"}]), PortCtx())
        assert r1.content == "PORT01-SPY1"
        assert spy1.calls == 1 and spy1.seen[0][0]["content"] == "PORT01-Q"

        spy2 = SpyLLM("PORT01-SPY2")
        reg.replace("llm", spy2)
        assert reg.resolve("llm") is spy2
        r2 = await reg.resolve("llm").complete(LLMRequest(messages=[]), PortCtx())
        assert r2.content == "PORT01-SPY2"
        assert spy2.calls == 1 and spy1.calls == 1

        seen: dict[str, Any] = {}
        builtin = _http_llm("PORT01-BUILTIN", seen)
        reg2 = PortRegistry()
        reg2.register("llm", builtin)
        assert reg2.resolve("llm") is builtin
        resp = await builtin.complete(
            LLMRequest(messages=[{"role": "user", "content": "PORT01-HTTP"}]), PortCtx())
        assert resp.content == "PORT01-BUILTIN"
        assert seen["url"].endswith("/chat/completions")
        assert seen["body"]["messages"][0]["content"] == "PORT01-HTTP"

        spy3 = SpyLLM("PORT01-ASAY")
        agent = Agent(PortArch([llm_step()])).use(ModelCapability(llm=spy3))
        answer = await agent.asay("PORT01-ASAY-Q")
        assert answer == "PORT01-ASAY"
        assert spy3.calls == 1
        assert any(m["content"] == "PORT01-ASAY-Q" for m in spy3.seen[0])
    run(scenario(), loop)


def test_port02_tool_dual(loop: Any) -> None:
    """PORT-02 tool 双路径。

    输入：自定义 SpyToolPort ×2；内置 ToolRegistry；用户工具函数带副作用计数。
    真实入口：PortRegistry.register/resolve/replace；SpyToolPort.register/catalog/call；
    ToolRegistry.call；Agent.asay→register_tool→tool port。
    精确断言：identity；自定义 register/catalog/call 计数与 ToolResult.value 精确；
    replace 后旧实例冻结；内置 registry 的 value/meta 精确；asay 副作用计数>0 且答复精确。
    失败判据：resolve 不 identity / 任一计数为 0 / value 不符即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyToolPort()
        reg.register("tool", spy1)
        assert reg.resolve("tool") is spy1
        spec = ToolSpec(name="t1", description="d", danger=DangerLevel.SAFE)
        reg.resolve("tool").register(spec, lambda **kw: kw)
        assert spy1.register_calls == 1
        assert reg.resolve("tool").catalog(level=2)[0].name == "t1"
        assert spy1.catalog_calls == 1
        result = await reg.resolve("tool").call(make_call("t1", {}), PortCtx())
        assert result.value == "SPY-TOOL-OK" and spy1.call_calls == 1
        assert spy1.effects == ["called:t1"]

        spy2 = SpyToolPort()
        reg.replace("tool", spy2)
        assert reg.resolve("tool") is spy2
        await reg.resolve("tool").call(make_call("t2", {}), PortCtx())
        assert spy2.call_calls == 1 and spy1.call_calls == 1

        builtin = ToolRegistry()
        hits: list[int] = []
        def impl(a: int) -> int:
            hits.append(a)
            return a + 1
        builtin.register(ToolSpec(name="inc", description="d",
                                  parameters_schema={"type": "object",
                                                     "properties": {"a": {"type": "integer"}}},
                                  danger=DangerLevel.SAFE), impl)
        br = await builtin.call(make_call("inc", {"a": 41}), PortCtx())
        assert br.ok and br.value == 42 and hits == [41]
        assert br.meta["danger"] == "SAFE"

        count = {"n": 0}
        def echo_tool(text: str) -> str:
            count["n"] += 1
            return f"ECHO:{text}"
        agent = Agent(PortArch([tool_step("echo", lambda t: {"text": t})]))
        agent.register_tool("echo", echo_tool)
        answer = await agent.asay("PORT02-ASAY")
        assert answer == "ECHO:PORT02-ASAY"
        assert count["n"] == 1
    run(scenario(), loop)


def test_port03_skill_dual(loop: Any, tmp_path: Any) -> None:
    """PORT-03 skill 双路径。

    输入：自定义 SpySkillPort ×2；内置 FileSkillProvider（真磁盘 manifest/SKILL.md）。
    真实入口：PortRegistry.register/resolve/replace；discover/catalog/body；
    FileSkillProvider.discover/body 读真文件。
    精确断言：identity；自定义三方法计数>0 且 manifest/body 文本精确；
    内置 provider discover 返回磁盘真实 name、body 返回磁盘正文原文。
    失败判据：identity 不符 / 计数 0 / 磁盘真实值未出现即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpySkillPort()
        reg.register("skill", spy1)
        assert reg.resolve("skill") is spy1
        manifests = reg.resolve("skill").discover()
        assert manifests[0].name == "spy-skill" and manifests[0].triggers == ("触发",)
        assert reg.resolve("skill").catalog()[0].version == "1.0"
        assert reg.resolve("skill").body("x") == "SPY-BODY:x"
        assert spy1.discover_calls == 2 and spy1.catalog_calls == 1 and spy1.body_calls == 1

        spy2 = SpySkillPort()
        reg.replace("skill", spy2)
        assert reg.resolve("skill") is spy2
        assert reg.resolve("skill").body("y") == "SPY-BODY:y"
        assert spy2.body_calls == 1 and spy1.body_calls == 1

        pkg = tmp_path / "port03-skill"
        pkg.mkdir()
        (pkg / "manifest.json").write_text(
            json.dumps({"name": "disk-skill", "version": "2.0",
                        "description": "磁盘技能", "triggers": ["磁盘"]}),
            encoding="utf-8")
        (pkg / "SKILL.md").write_text("PORT03-DISK-BODY", encoding="utf-8")
        builtin = FileSkillProvider(tmp_path)
        reg2 = PortRegistry()
        reg2.register("skill", builtin)
        assert reg2.resolve("skill") is builtin
        found = builtin.discover()
        assert [m.name for m in found] == ["disk-skill"]
        assert builtin.body("port03-skill") == "PORT03-DISK-BODY"
    run(scenario(), loop)


def test_port04_rag_dual(loop: Any) -> None:
    """PORT-04 rag 双路径。

    输入：自定义 SpyRAG ×2；内置 RAGPipeline（默认五段真实组件）。
    真实入口：PortRegistry.register/resolve/replace；ingest/retrieve；
    RAGPipeline.ingest/retrieve 真入库真检索。
    精确断言：identity；自定义计数与 retrieve 返回原文精确；内置入库块数>0 且
    retrieve 命中文本含入库原文；replace 后旧实例冻结。
    失败判据：identity 不符 / 计数 0 / 检索不含原文即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyRAG(["PORT04-HIT"])
        reg.register("rag", spy1)
        assert reg.resolve("rag") is spy1
        n = await reg.resolve("rag").ingest([{"doc_id": "d", "text": "t"}], PortCtx())
        assert n == 1 and spy1.ingest_calls == 1
        hits = await reg.resolve("rag").retrieve("PORT04-Q", PortCtx())
        assert hits == ["PORT04-HIT"] and spy1.retrieve_calls == 1
        assert spy1.last_query == "PORT04-Q"

        spy2 = SpyRAG(["PORT04-HIT2"])
        reg.replace("rag", spy2)
        assert reg.resolve("rag") is spy2
        assert await reg.resolve("rag").retrieve("q", PortCtx()) == ["PORT04-HIT2"]
        assert spy2.retrieve_calls == 1 and spy1.retrieve_calls == 1

        builtin = _builtin_rag()
        reg2 = PortRegistry()
        reg2.register("rag", builtin)
        assert reg2.resolve("rag") is builtin
        docs = [Document(doc_id="p04", text="PORT04-BUILTIN-HIT 检索增强", meta={})]
        chunks = await builtin.ingest(docs, PortCtx())
        assert chunks > 0
        found = await builtin.retrieve("PORT04-BUILTIN-HIT", PortCtx())
        assert any("PORT04-BUILTIN-HIT" in str(h) for h in found)
    run(scenario(), loop)


def test_port05_memory_dual(loop: Any) -> None:
    """PORT-05 memory 双路径（重点：Agent.asay 真消费）。

    输入：自定义 SpyMemory ×2；内置 MemoryStore；自定义 UserMemory + MemoryCapability。
    真实入口：PortRegistry.register/resolve/replace；read/write/forget；
    Agent.asay→MemoryCapability.recall_before/write_after→LLM 实收 system messages。
    精确断言：identity；自定义三方法计数与值精确；内置 store read==write 值；
    asay 两轮后 UserMemory.ingest_calls>0、recall_calls>0，且 LLM system 含召回原文。
    失败判据：identity 不符 / 计数 0 / LLM 实收 system 不含写入文本即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyMemory()
        reg.register("memory", spy1)
        assert reg.resolve("memory") is spy1
        await reg.resolve("memory").write("ns", "k", "PORT05-V", PortCtx())
        assert await reg.resolve("memory").read("ns", "k", PortCtx()) == "PORT05-V"
        await reg.resolve("memory").forget("ns", "k", PortCtx())
        assert await reg.resolve("memory").read("ns", "k", PortCtx()) is None
        assert (spy1.write_calls, spy1.read_calls, spy1.forget_calls) == (1, 2, 1)

        spy2 = SpyMemory()
        reg.replace("memory", spy2)
        assert reg.resolve("memory") is spy2
        await reg.resolve("memory").write("n", "k", "V2", PortCtx())
        assert spy2.write_calls == 1 and spy1.write_calls == 1

        builtin = MemoryStore()
        reg2 = PortRegistry()
        reg2.register("memory", builtin)
        assert reg2.resolve("memory") is builtin
        await builtin.write("ns", "k", "PORT05-BUILTIN", PortCtx())
        assert await builtin.read("ns", "k", PortCtx()) == "PORT05-BUILTIN"

        custom = UserMemory()
        llm = RecordingLLM(answer="好的")
        agent = Agent(PortArch([llm_step()])).use(ModelCapability(llm=llm)).use(
            MemoryCapability(manager=ContextMemoryManager(memory=custom)))  # type: ignore[arg-type]
        await agent.asay("记住 PORT05-MEM-PREF")
        await agent.asay("PORT05-MEM-PREF 是什么")
        assert custom.ingest_calls > 0 and custom.recall_calls > 0
        assert "PORT05-MEM-PREF" in llm.system_text()
    run(scenario(), loop)


def test_port06_store_dual(loop: Any) -> None:
    """PORT-06 store 双路径。

    输入：自定义 SpyStore ×2；内置 InMemoryStorePort。
    真实入口：PortRegistry.register/resolve/replace；save/load。
    精确断言：identity；自定义 save/load 计数与快照内容精确；内置 save→load 值精确；
    replace 后旧实例冻结。失败判据：identity 不符 / 计数 0 / 快照不符即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyStore()
        reg.register("store", spy1)
        assert reg.resolve("store") is spy1
        await reg.resolve("store").save("s1", {"k": "PORT06"})
        assert await reg.resolve("store").load("s1") == {"k": "PORT06"}
        assert (spy1.save_calls, spy1.load_calls) == (1, 1)

        spy2 = SpyStore()
        reg.replace("store", spy2)
        assert reg.resolve("store") is spy2
        await reg.resolve("store").save("s2", {"k": "V2"})
        assert spy2.save_calls == 1 and spy1.save_calls == 1

        builtin = InMemoryStorePort()
        reg2 = PortRegistry()
        reg2.register("store", builtin)
        assert reg2.resolve("store") is builtin
        await builtin.save("s3", {"a": 1})
        assert await builtin.load("s3") == {"a": 1}
        assert await builtin.load("missing") is None
    run(scenario(), loop)


def test_port07_sandbox_dual(loop: Any) -> None:
    """PORT-07 sandbox 双路径。

    输入：自定义 SpySandbox ×2；内置 LocalSandbox(allow_sync=True)。
    真实入口：PortRegistry.register/resolve/replace；run(fn,args,ctx) 真执行函数。
    精确断言：identity；自定义 run 计数>0 且返回值=函数实算；内置返回值精确；
    replace 后旧实例冻结。失败判据：identity 不符 / 计数 0 / 返回值不符即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpySandbox()
        reg.register("sandbox", spy1)
        assert reg.resolve("sandbox") is spy1
        out = await reg.resolve("sandbox").run(lambda a, b: a + b,
                                               {"a": 20, "b": 22}, PortCtx())
        assert out == 42 and spy1.run_calls == 1

        spy2 = SpySandbox()
        reg.replace("sandbox", spy2)
        assert reg.resolve("sandbox") is spy2
        assert await reg.resolve("sandbox").run(lambda a: a * 2, {"a": 3}, PortCtx()) == 6
        assert spy2.run_calls == 1 and spy1.run_calls == 1

        builtin = LocalSandbox(allow_sync=True)
        reg2 = PortRegistry()
        reg2.register("sandbox", builtin)
        assert reg2.resolve("sandbox") is builtin
        assert await builtin.run(lambda a: a + 1, {"a": 9}, PortCtx()) == 10
    run(scenario(), loop)


def test_port08_audit_dual(loop: Any) -> None:
    """PORT-08 audit 双路径。

    输入：自定义 CountingAudit ×2（包装真实 AuditLogger 保验链）；内置 AuditLogger。
    真实入口：PortRegistry.register/resolve/replace；record(event,ctx) 追加事件。
    精确断言：identity；自定义 record 计数与 events() 中 action 精确；内置 events 长度与
    action 精确；replace 后旧实例冻结。失败判据：identity 不符 / 计数 0 / 事件缺失即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = CountingAudit()
        reg.register("audit", spy1)
        assert reg.resolve("audit") is spy1
        await reg.resolve("audit").record(
            {"action": "PORT08-A", "who": "u", "when": 1, "cost": 0.0,
             "data_touched": (), "rationale": "r"}, PortCtx())
        assert spy1.record_calls == 1
        assert len(spy1.events()) == 1 and spy1.events()[0].action == "PORT08-A"

        spy2 = CountingAudit()
        reg.replace("audit", spy2)
        assert reg.resolve("audit") is spy2
        await reg.resolve("audit").record(
            {"action": "PORT08-B", "who": "u", "when": 1, "cost": 0.0,
             "data_touched": (), "rationale": "r"}, PortCtx())
        assert spy2.record_calls == 1 and spy1.record_calls == 1

        builtin = AuditLogger()
        reg2 = PortRegistry()
        reg2.register("audit", builtin)
        assert reg2.resolve("audit") is builtin
        await builtin.record(
            {"action": "PORT08-BUILTIN", "who": "u", "when": 1, "cost": 0.0,
             "data_touched": (), "rationale": "r"}, PortCtx())
        assert len(builtin.events()) == 1
        assert builtin.events()[0].action == "PORT08-BUILTIN"
    run(scenario(), loop)


def test_port09_eval_dual(loop: Any) -> None:
    """PORT-09 eval 双路径。

    输入：自定义 SpyEval ×2；内置 RuleEval。
    真实入口：PortRegistry.register/resolve/replace；score(case_id,output,ctx)。
    精确断言：identity；自定义 score 计数与浮点返回精确；内置对 ("x","x") 评 1.0、
    对 ("x","y") 评 0.0；replace 后旧实例冻结。失败判据：identity 不符 / 分数不符即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyEval(0.25)
        reg.register("eval", spy1)
        assert reg.resolve("eval") is spy1
        assert await reg.resolve("eval").score("c", "o", PortCtx()) == 0.25
        assert spy1.score_calls == 1

        spy2 = SpyEval(0.75)
        reg.replace("eval", spy2)
        assert reg.resolve("eval") is spy2
        assert await reg.resolve("eval").score("c", "o", PortCtx()) == 0.75
        assert spy2.score_calls == 1 and spy1.score_calls == 1

        builtin = RuleEval()
        reg2 = PortRegistry()
        reg2.register("eval", builtin)
        assert reg2.resolve("eval") is builtin
        assert await builtin.score("c", ("PORT09", "PORT09"), PortCtx()) == 1.0
        assert await builtin.score("c", ("PORT09", "OTHER"), PortCtx()) == 0.0
    run(scenario(), loop)


def test_port10_telemetry_dual(loop: Any) -> None:
    """PORT-10 telemetry 双路径（重点：Agent.asay span 真装填）。

    输入：自定义 CustomTelemetry ×2（带 _register/_replace 计数）；内置 NoopTelemetry。
    真实入口：PortRegistry.register/resolve/replace；span/metric/log；
    Agent.asay→Trace→自定义 telemetry 的 _register/_replace。
    精确断言：identity；自定义 metric/log/span 留痕精确；内置 metrics/logs/spans 精确；
    asay 后自定义收到名为 llm 的 span、register_calls>0、replace_calls>0。
    失败判据：identity 不符 / span 未登记 / 计数 0 即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = CustomTelemetry()
        reg.register("telemetry", spy1)
        assert reg.resolve("telemetry") is spy1
        with reg.resolve("telemetry").span("PORT10-SPAN", PortCtx()) as span:
            assert span.name == "PORT10-SPAN"
        reg.resolve("telemetry").metric("m", 3.0, {"k": "v"})
        reg.resolve("telemetry").log({"event": "PORT10-LOG"})
        assert spy1.register_calls == 1 and spy1.replace_calls == 1
        assert spy1.metrics() == (("m", 3.0, {"k": "v"}),)
        assert spy1.logs()[0]["event"] == "PORT10-LOG"

        spy2 = CustomTelemetry()
        reg.replace("telemetry", spy2)
        assert reg.resolve("telemetry") is spy2
        reg.resolve("telemetry").metric("m2", 1.0)
        assert spy2.metrics() == (("m2", 1.0, None),) and spy1.metrics() != spy2.metrics()

        builtin = NoopTelemetry()
        reg2 = PortRegistry()
        reg2.register("telemetry", builtin)
        assert reg2.resolve("telemetry") is builtin
        with builtin.span("BUILTIN-SPAN", PortCtx()):
            pass
        builtin.metric("bm", 2.0)
        builtin.log({"e": "BUILTIN-LOG"})
        assert builtin.spans()[0].name == "BUILTIN-SPAN"
        assert builtin.metrics() == (("bm", 2.0, None),)
        assert builtin.logs()[0]["e"] == "BUILTIN-LOG"

        asay_tel = CustomTelemetry()
        agent = Agent(PortArch([llm_step()]), session_id="port10").use(
            ModelCapability(llm=RecordingLLM(answer="PORT10-OK"))
        ).use(ObservabilityCapability(telemetry=asay_tel))  # type: ignore[arg-type]
        assert await agent.asay("x") == "PORT10-OK"
        names = [s.name for s in asay_tel.spans()]
        assert any(n == "llm" for n in names)
        assert asay_tel.register_calls > 0 and asay_tel.replace_calls > 0
    run(scenario(), loop)


def test_port11_human_dual(loop: Any) -> None:
    """PORT-11 human 双路径。

    输入：自定义 SpyHuman ×2；内置 ConsoleHumanPort(answerer=...)。
    真实入口：PortRegistry.register/resolve/replace；ask(question,options,ctx)。
    精确断言：identity；自定义 ask 计数>0 且返回注入答案；内置返回 answerer 精确值；
    replace 后旧实例冻结。失败判据：identity 不符 / 答案不符 / 计数 0 即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyHuman("PORT11-YES")
        reg.register("human", spy1)
        assert reg.resolve("human") is spy1
        answer = await reg.resolve("human").ask("q?", ["a", "b"], PortCtx())
        assert answer == "PORT11-YES" and spy1.ask_calls == 1

        spy2 = SpyHuman("PORT11-NO")
        reg.replace("human", spy2)
        assert reg.resolve("human") is spy2
        assert await reg.resolve("human").ask("q?", None, PortCtx()) == "PORT11-NO"
        assert spy2.ask_calls == 1 and spy1.ask_calls == 1

        seen: list[str] = []
        builtin = ConsoleHumanPort(answerer=lambda q, o: (seen.append(q) or "PORT11-BUILTIN"))
        reg2 = PortRegistry()
        reg2.register("human", builtin)
        assert reg2.resolve("human") is builtin
        assert await builtin.ask("内置问题?", ["approve"], PortCtx()) == "PORT11-BUILTIN"
        assert seen == ["内置问题?"]
    run(scenario(), loop)


def test_port12_mcp_dual(loop: Any) -> None:
    """PORT-12 mcp 双路径（重点：Agent.asay 真消费 MCP transport）。

    输入：自定义 SpyMCPTransport；内置 MCPManager（框架 MCPPort 实现）+ 同 transport。
    真实入口：PortRegistry.register/resolve/replace（自定义 MCPPort 对象=MCPManager）；
    list_tools/call_tool/ping；Agent.asay→ToolsCapability.connect_mcp→tool port→MCP tool。
    精确断言：identity；自定义与内置三方法计数/返回值精确；asay 后 transport.call_calls>0
    且答复="MCP-ECHO:PORT12-Q"；全名 mcp__srv__echo 真进执行链。
    失败判据：identity 不符 / transport 计数 0 / 答复不符即红。
    """
    async def scenario() -> None:
        transport1 = SpyMCPTransport()
        mgr1 = MCPManager(ToolRegistry(), transport1)  # type: ignore[arg-type]
        reg = PortRegistry()
        reg.register("mcp", mgr1)
        assert reg.resolve("mcp") is mgr1
        specs = await reg.resolve("mcp").list_tools("srv", PortCtx())  # type: ignore[attr-defined]
        assert specs[0].name == "echo"
        assert await reg.resolve("mcp").call_tool(  # type: ignore[attr-defined]
            "srv", "echo", {"text": "x"}, PortCtx()) == "MCP-ECHO:x"
        assert await reg.resolve("mcp").ping("srv", PortCtx()) is True  # type: ignore[attr-defined]
        assert (transport1.list_calls, transport1.call_calls, transport1.ping_calls) == (1, 1, 1)

        transport2 = SpyMCPTransport()
        mgr2 = MCPManager(ToolRegistry(), transport2)  # type: ignore[arg-type]
        reg.replace("mcp", mgr2)
        assert reg.resolve("mcp") is mgr2
        await reg.resolve("mcp").ping("srv", PortCtx())  # type: ignore[attr-defined]
        assert transport2.ping_calls == 1 and transport1.ping_calls == 1

        # 内置 MCPPort（MCPManager）真 connect：工具真注入执行链 registry
        wired = ToolRegistry()
        builtin_mgr = MCPManager(wired, transport1)  # type: ignore[arg-type]
        injected = await builtin_mgr.connect("srv", PortCtx())
        assert injected[0].name == "mcp__srv__echo"
        assert [s.name for s in wired.catalog(level=1)] == ["mcp__srv__echo"]

        asay_transport = SpyMCPTransport()
        agent = Agent(PortArch([tool_step("mcp__srv__echo",
                                           lambda t: {"text": t})])).use(
            ToolsCapability(mcp=asay_transport, mcp_servers=("srv",)))  # type: ignore[arg-type]
        answer = await agent.asay("PORT12-Q")
        assert answer == "MCP-ECHO:PORT12-Q"
        assert asay_transport.list_calls > 0 and asay_transport.call_calls > 0
    run(scenario(), loop)


def test_port13_event_stream_dual(loop: Any) -> None:
    """PORT-13 event_stream 双路径（重点：Agent.asay 真 publish）。

    输入：自定义 SpyEventStream ×2；内置 InMemoryEventStream。
    真实入口：PortRegistry.register/resolve/replace；publish/subscribe/topics；
    Agent(stream=自定义)→bind_stream_projection→publish(EventEnvelope)。
    精确断言：identity；自定义 publish/subscribe/topics 计数与事件 data 精确；
    内置 subscribe 收到真实 seq/topic/data；asay 后自定义 publish_calls>0。
    失败判据：identity 不符 / publish 计数 0 / 事件内容不符即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyEventStream()
        reg.register("event_stream", spy1)
        assert reg.resolve("event_stream") is spy1
        event = EventEnvelope(seq=7, topic="port13", ts=1.0,
                              type="evt", data={"k": "PORT13"})
        reg.resolve("event_stream").publish(event)  # type: ignore[attr-defined]
        got = [e async for e in reg.resolve("event_stream").subscribe(  # type: ignore[attr-defined]
            "port13", since=6)]
        assert got == [event] and got[0].data["k"] == "PORT13"
        assert reg.resolve("event_stream").topics() == ["port13"]  # type: ignore[attr-defined]
        assert (spy1.publish_calls, spy1.subscribe_calls, spy1.topics_calls) == (1, 1, 1)

        spy2 = SpyEventStream()
        reg.replace("event_stream", spy2)
        assert reg.resolve("event_stream") is spy2
        reg.resolve("event_stream").publish(EventEnvelope(  # type: ignore[attr-defined]
            seq=1, topic="t2", ts=2.0, type="e", data={"k": "V2"}))
        assert spy2.publish_calls == 1 and spy1.publish_calls == 1

        builtin = InMemoryEventStream()
        reg2 = PortRegistry()
        reg2.register("event_stream", builtin)
        assert reg2.resolve("event_stream") is builtin
        builtin.publish(EventEnvelope(seq=9, topic=STREAM_TOPIC, ts=3.0,
                                      type="evt", data={"k": "PORT13-BUILTIN"}))
        items = [e async for e in builtin.subscribe(STREAM_TOPIC, since=8)]
        assert items[0].seq == 9 and items[0].data["k"] == "PORT13-BUILTIN"
        assert STREAM_TOPIC in builtin.topics()

        asay_stream = SpyEventStream()
        agent = Agent(PortArch([llm_step()]), stream=asay_stream).use(  # type: ignore[arg-type]
            ModelCapability(llm=RecordingLLM(answer="PORT13-OK")))
        assert await agent.asay("x") == "PORT13-OK"
        assert asay_stream.publish_calls > 0
        assert all(e.topic == STREAM_TOPIC for e in asay_stream.events)
    run(scenario(), loop)


def test_port14_disclosure_dual(loop: Any) -> None:
    """PORT-14 disclosure 双路径。

    输入：自定义 SpyDisclosure ×2；内置 ToolIndex（真 ToolRegistry 目录）。
    真实入口：PortRegistry.register/resolve/replace；disclose(names,skills)；
    ToolIndex.disclose 真投影 L1/L2。
    精确断言：identity；自定义 disclose 计数与 pkt.token_cost/l1.name 精确；
    内置 pkt.l1 schema 为空（L1 红线）、l2 含命中 spec；replace 后旧实例冻结。
    失败判据：identity 不符 / 计数 0 / L1 泄漏 schema 或 L2 缺失即红。
    """
    async def scenario() -> None:
        reg = PortRegistry()
        spy1 = SpyDisclosure()
        reg.register("disclosure", spy1)
        assert reg.resolve("disclosure") is spy1
        pkt = reg.resolve("disclosure").disclose(["a"], [])  # type: ignore[attr-defined]
        assert pkt.token_cost == 7 and pkt.l1[0].name == "spy-tool"
        assert spy1.disclose_calls == 1 and spy1.last_names == ["a"]

        spy2 = SpyDisclosure()
        reg.replace("disclosure", spy2)
        assert reg.resolve("disclosure") is spy2
        reg.resolve("disclosure").disclose(["b"], [])  # type: ignore[attr-defined]
        assert spy2.disclose_calls == 1 and spy1.disclose_calls == 1

        reg_tools = ToolRegistry()
        spec = ToolSpec(name="p14", description="披露工具",
                        parameters_schema={"type": "object",
                                           "properties": {"a": {"type": "integer"}}},
                        danger=DangerLevel.SAFE)
        reg_tools.register(spec, lambda **kw: kw)
        builtin = ToolIndex(reg_tools)
        reg2 = PortRegistry()
        reg2.register("disclosure", builtin)
        assert reg2.resolve("disclosure") is builtin
        bp = builtin.disclose(["p14"], [])
        assert bp.l1[0].name == "p14" and bp.l1[0].parameters_schema == {}
        assert bp.l2[0].name == "p14" and "a" in bp.l2[0].parameters_schema["properties"]
    run(scenario(), loop)


# =========================================================================== #
# CAP-01~12
# =========================================================================== #
def test_cap01_cognition_dual(loop: Any) -> None:
    """CAP-01 cognition 双路径。

    输入：自定义 CountingIntent；默认 CascadeIntentStrategy。
    真实入口：CognitionCapability.install→Registry.resolve('intent')；
    Agent.recognize_intent→cap.recognize→strategy.recognize（真转调）。
    精确断言：resolve is 注入策略；策略 calls=1、实收 text 精确、返回 label/confidence/gaps 精确；
    默认策略对任意输入返回 label='unknown'、confidence=0.0。
    失败判据：identity 不符 / calls=0 / 返回值不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        strategy = CountingIntent("CAP01-CUSTOM")
        cap = CognitionCapability(strategy=strategy)
        cap.install(None, reg)
        assert reg.resolve("intent") is strategy

        agent = Agent(PortArch([llm_step()])).use(cap)
        result = agent.recognize_intent("CAP01-Q")
        assert strategy.calls == 1 and strategy.last_text == "CAP01-Q"
        assert result.label == "CAP01-CUSTOM" and result.confidence == 0.99
        assert result.gaps == ("gap",) and result.trace == ("custom",)

        reg2 = Registry()
        default_cap = CognitionCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("intent") is default_cap.strategy
        agent2 = Agent(PortArch([llm_step()])).use(default_cap)
        out = agent2.recognize_intent("随便输入CAP01")
        assert out.label == "unknown" and out.confidence == 0.0
    run(scenario(), loop)


def test_cap02_reflection_dual(loop: Any) -> None:
    """CAP-02 reflection 双路径。

    输入：自定义 CountingReflector；默认 ThresholdReflector。
    真实入口：ReflectionCapability.install→Registry.resolve('reflection')；
    Agent.evaluate→cap.critique→reflector.critique(draft,goal,ctx) 三参真传。
    精确断言：resolve is cap（其 reflector is 注入实例）；calls=1、draft/goal 精确、
    Critique.score/passed/comments 精确；默认评审对非空草稿 passed=True/score=1.0。
    失败判据：identity 不符 / 参数不符 / Critique 不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        reflector = CountingReflector(0.42)
        cap = ReflectionCapability(reflector=reflector)
        cap.install(None, reg)
        assert reg.resolve("reflection") is cap and cap.reflector is reflector

        agent = Agent(PortArch([llm_step()])).use(cap)
        out = agent.evaluate("CAP02-DRAFT", "CAP02-GOAL")
        assert reflector.calls == 1
        assert reflector.args[0] == "CAP02-DRAFT" and reflector.args[1] == "CAP02-GOAL"
        assert out.score == 0.42 and out.passed is False
        assert out.comments == "custom-critique"

        reg2 = Registry()
        default_cap = ReflectionCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("reflection") is default_cap
        d = default_cap.critique("CAP02-ANY", "")
        assert d.passed is True and d.score == 1.0
    run(scenario(), loop)


def test_cap03_memory_dual(loop: Any) -> None:
    """CAP-03 memory 双路径（asay 真消费）。

    输入：自定义 UserMemory（真词面索引）；默认 MemoryStore。
    真实入口：MemoryCapability.install→Registry.resolve('memory')；
    Agent.asay→recall_before/write_after→LLM 实收 system messages。
    精确断言：resolve is 注入后端；自定义 ingest_calls>0、recall_calls>0、LLM system 含原文；
    默认 manager.memory.tier_count()>0。
    失败判据：identity 不符 / 计数 0 / LLM system 不含原文即红。
    """
    async def scenario() -> None:
        reg = Registry()
        custom = UserMemory()
        cap = MemoryCapability(manager=ContextMemoryManager(memory=custom))  # type: ignore[arg-type]
        cap.install(None, reg)
        assert reg.resolve("memory") is custom

        llm = RecordingLLM(answer="CAP03-OK")
        agent = Agent(PortArch([llm_step()])).use(ModelCapability(llm=llm)).use(cap)
        assert await agent.asay("记住 CAP03-FACT") == "CAP03-OK"
        assert await agent.asay("CAP03-FACT 是什么") == "CAP03-OK"
        assert custom.ingest_calls > 0 and custom.recall_calls > 0
        assert "CAP03-FACT" in llm.system_text()

        reg2 = Registry()
        default_cap = MemoryCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("memory") is default_cap.manager.memory
        agent2 = Agent(PortArch([llm_step()])).use(
            ModelCapability(llm=RecordingLLM(answer="CAP03-D"))
        ).use(default_cap)
        assert await agent2.asay("默认 CAP03-FACT") == "CAP03-D"
        assert default_cap.manager.memory.tier_count() > 0
    run(scenario(), loop)


def test_cap04_eval_dual(loop: Any) -> None:
    """CAP-04 eval 双路径（asay 真消费）。

    输入：自定义 CountingEvaluator（返回 0.75）；默认 RuleEval。
    真实入口：EvalCapability.install→Registry.resolve('eval')；
    Agent.asay→Invoke(port='eval')→port handler op='score'→evaluator.score 三参真传；
    Agent.score_case 二次入口。
    精确断言：resolve is cap；evaluator.calls=2、case_id/output 精确；asay 答复='0.75'；
    score_case 返回 0.75；默认 RuleEval 对 ("x","x") 返回 1.0。
    失败判据：identity 不符 / 参数不符 / 分数不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        evaluator = CountingEvaluator(0.75)
        cap = EvalCapability(evaluator=evaluator)
        cap.install(None, reg)
        assert reg.resolve("eval") is cap

        agent = Agent(PortArch([
            port_step("eval", lambda t: {"case_id": "CAP04-C", "output": "CAP04-O"})
        ])).use(cap)
        answer = await agent.asay("x")
        assert answer == "0.75"
        assert evaluator.calls == 1
        assert evaluator.args[0] == "CAP04-C" and evaluator.args[1] == "CAP04-O"
        assert await agent.score_case("CAP04-C2", "o") == 0.75
        assert evaluator.calls == 2 and evaluator.args[0] == "CAP04-C2"

        reg2 = Registry()
        default_cap = EvalCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("eval") is default_cap
        agent2 = Agent(PortArch([llm_step()])).use(default_cap)
        assert await agent2.score_case("c", ("CAP04", "CAP04")) == 1.0
    run(scenario(), loop)


def test_cap05_model_dual(loop: Any) -> None:
    """CAP-05 model 双路径。

    输入：自定义 SpyLLM；内置 OpenAICompatLLM（HTTP MockTransport 真接收端）。
    真实入口：ModelCapability.install→Registry.resolve('llm')；
    Agent.asay→gateway.dispatch→LLMPort.complete。
    精确断言：resolve is 注入 LLM；自定义答复与实收 messages 精确、calls=1；
    内置 HTTP 接收端 body.messages 精确、答复精确。
    失败判据：identity 不符 / LLM 实收 messages 不符 / 答复不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        spy = SpyLLM("CAP05-CUSTOM")
        cap = ModelCapability(llm=spy)
        cap.install(None, reg)
        assert reg.resolve("llm") is spy
        agent = Agent(PortArch([llm_step()])).use(cap)
        assert await agent.asay("CAP05-Q") == "CAP05-CUSTOM"
        assert spy.calls == 1 and spy.seen[0][0]["content"] == "CAP05-Q"

        seen: dict[str, Any] = {}
        builtin = _http_llm("CAP05-BUILTIN", seen)
        reg2 = Registry()
        cap2 = ModelCapability(llm=builtin)
        cap2.install(None, reg2)
        assert reg2.resolve("llm") is builtin
        agent2 = Agent(PortArch([llm_step()])).use(cap2)
        assert await agent2.asay("CAP05-HTTP") == "CAP05-BUILTIN"
        assert seen["body"]["messages"][0]["content"] == "CAP05-HTTP"
    run(scenario(), loop)


def test_cap06_observability_dual(loop: Any) -> None:
    """CAP-06 observability 双路径（asay span 真装填）。

    输入：自定义 CustomTelemetry；默认 NoopTelemetry。
    真实入口：ObservabilityCapability.install→Registry.resolve('telemetry')；
    Agent.asay→Trace→telemetry._register/_replace。
    精确断言：resolve is 注入 telemetry；自定义 spans 含 llm、register_calls>0；
    默认 agent.trace.spans() 含 llm。失败判据：identity 不符 / 无 llm span 即红。
    """
    async def scenario() -> None:
        reg = Registry()
        tel = CustomTelemetry()
        cap = ObservabilityCapability(telemetry=tel)  # type: ignore[arg-type]
        cap.install(None, reg)
        assert reg.resolve("telemetry") is tel

        agent = Agent(PortArch([llm_step()]), session_id="cap06").use(
            ModelCapability(llm=RecordingLLM(answer="CAP06-OK"))
        ).use(cap)
        assert await agent.asay("x") == "CAP06-OK"
        assert any(s.name == "llm" for s in tel.spans())
        assert tel.register_calls > 0 and tel.replace_calls > 0

        reg2 = Registry()
        default_cap = ObservabilityCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("telemetry") is default_cap.telemetry
        agent2 = Agent(PortArch([llm_step()]), session_id="cap06-default").use(
            ModelCapability(llm=RecordingLLM(answer="CAP06-D"))
        ).use(default_cap)
        assert await agent2.asay("x") == "CAP06-D"
        assert any(s.name == "llm" for s in agent2.trace.spans())
    run(scenario(), loop)


def test_cap07_prompt_dual(loop: Any) -> None:
    """CAP-07 prompt 双路径（LLM 实收 system 精确）。

    输入：用户自定义 PromptTemplate('cap07.custom')；默认 default.system 模板。
    真实入口：PromptCapability.install→Registry.resolve('prompt:<name>')；
    Agent(system=模板)+asay→_call_llm→prompt_cap.render→LLM 实收 messages。
    精确断言：resolve is 注入模板；LLM system 含自定义渲染文本；默认 system 含
    '你是中文助手'。失败判据：identity 不符 / LLM system 不含模板渲染结果即红。
    """
    async def scenario() -> None:
        reg = Registry()
        tpl = PromptTemplate(name="cap07.custom",
                             system=Template("CAP07-SYSTEM ${input}"),
                             user=Template("${input}"))
        cap = PromptCapability(templates=(tpl,))
        cap.install(None, reg)
        assert reg.resolve("prompt:cap07.custom") is tpl

        llm = RecordingLLM(answer="CAP07-OK")
        agent = Agent(PortArch([llm_step()]), system=tpl).use(
            ModelCapability(llm=llm)
        ).use(cap)
        assert await agent.asay("CAP07-Q") == "CAP07-OK"
        assert "CAP07-SYSTEM CAP07-Q" in llm.system_text()

        reg2 = Registry()
        default_cap = PromptCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("prompt:default.system").name == "default.system"
        llm2 = RecordingLLM(answer="CAP07-D")
        agent2 = Agent(PortArch([llm_step()])).use(
            ModelCapability(llm=llm2)
        ).use(default_cap)
        assert await agent2.asay("CAP07-Q2") == "CAP07-D"
        assert "你是中文助手" in llm2.system_text()
    run(scenario(), loop)


def test_cap08_rag_dual(loop: Any) -> None:
    """CAP-08 rag 双路径（asay 真消费 pipeline）。

    输入：自定义 SpyRAG；内置 RAGPipeline（默认五段真组件）。
    真实入口：RAGCapability.install→Registry.resolve('rag')；
    Agent.asay→Invoke(port='rag')→port handler op='retrieve'→pipeline.retrieve。
    精确断言：resolve is cap；自定义 calls=1、last_query 精确、答复含命中原文；
    默认 pipeline 经 cap.retrieve/ingest 真入库真检索，命中原文精确。
    失败判据：identity 不符 / 计数 0 / 检索不含命中原文即红。
    """
    async def scenario() -> None:
        reg = Registry()
        custom = SpyRAG(["CAP08-CUSTOM-HIT"])
        cap = RAGCapability(pipeline=custom)  # type: ignore[arg-type]
        cap.install(None, reg)
        assert reg.resolve("rag") is cap

        agent = Agent(PortArch([
            port_step("rag", lambda t: {"query": t})
        ])).use(cap)
        answer = await agent.asay("CAP08-Q")
        assert custom.retrieve_calls == 1 and custom.last_query == "CAP08-Q"
        assert "CAP08-CUSTOM-HIT" in answer

        builtin = _builtin_rag()
        chunks = await builtin.ingest(
            [Document(doc_id="c8", text="CAP08-BUILTIN-HIT 检索", meta={})], PortCtx())
        assert chunks > 0
        reg2 = Registry()
        default_cap = RAGCapability(pipeline=builtin)
        default_cap.install(None, reg2)
        assert reg2.resolve("rag") is default_cap
        found = await default_cap.retrieve("CAP08-BUILTIN-HIT", PortCtx())
        assert any("CAP08-BUILTIN-HIT" in str(h) for h in found)
        assert await default_cap.ingest(
            [{"doc_id": "c8b", "text": "CAP08-BUILTIN-IN"}], PortCtx()) == 1
    run(scenario(), loop)


def test_cap09_resilience_dual(loop: Any) -> None:
    """CAP-09 resilience 双路径（asay 真经过 plugin）。

    输入：自定义 CountingPlugin（函数式 call 变换器）；默认无策略。
    真实入口：ResilienceCapability.install→Registry.resolve('reliability_executor')；
    Agent.asay→_derive_call→cap.wrap→ReliabilityExecutor→transform(core)。
    精确断言：resolve 出的 factory 真调用一次→transform_calls=1；asay 后 transform_calls=2、
    invoke_calls>0、答复精确；默认 plugins()==() 且 asay 正常。
    失败判据：plugin 未被真包裹 / 计数 0 / 答复不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        plugin = CountingPlugin()
        cap = ResilienceCapability(extra_plugins=(plugin,))  # type: ignore[arg-type]
        cap.install(None, reg)
        factory = reg.resolve("reliability_executor")

        async def core(inv: Any) -> Any:
            return Result.success({"content": "CAP09-FACTORY"})

        executor = factory(core)
        inv = Invocation(Invoke(port="llm", payload={"messages": []}), {}, object())
        ran = await executor.run_invocation(inv)
        assert ran.ok and plugin.transform_calls == 1 and plugin.invoke_calls == 1
        assert plugin in cap.plugins()

        agent = Agent(PortArch([llm_step()])).use(cap).use(
            ModelCapability(llm=SpyLLM("CAP09-OK")))
        assert await agent.asay("CAP09-Q") == "CAP09-OK"
        assert plugin.transform_calls == 2 and plugin.invoke_calls > 0

        reg2 = Registry()
        default_cap = ResilienceCapability()
        default_cap.install(None, reg2)
        assert default_cap.plugins() == ()
        agent2 = Agent(PortArch([llm_step()])).use(default_cap).use(
            ModelCapability(llm=SpyLLM("CAP09-D")))
        assert await agent2.asay("x") == "CAP09-D"
    run(scenario(), loop)


def test_cap10_security_dual(loop: Any) -> None:
    """CAP-10 security 双路径（asay 真走沙盒+审计）。

    输入：自定义 CountingSandbox / CountingAudit / SpyHuman；默认三 Port。
    真实入口：SecurityCapability.install→Registry.resolve('sandbox'/'audit'/'human')；
    Agent.asay→tool port→HIGH danger→sandbox.run+audit.record（验链包装）。
    精确断言：三个 resolve 均 is 注入实例；asay 答复='CAP10:7'、sandbox.run_calls>0、
    audit.record_calls>0；默认 SAFE 工具 asay 答复精确。
    失败判据：identity 不符 / 沙盒或审计计数 0 / 答复不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        sandbox = SpySandbox()
        audit = CountingAudit()
        human = SpyHuman()
        sec = SecurityCapability(sandbox=sandbox, audit=audit, human=human)  # type: ignore[arg-type]
        sec.install(None, reg)
        assert reg.resolve("sandbox") is sandbox
        assert reg.resolve("audit") is audit
        assert reg.resolve("human") is human

        def high_tool(x: int) -> str:
            return f"CAP10:{x}"

        agent = Agent(PortArch([tool_step(
            "cap10_tool", lambda t: {"x": 7}, danger="HIGH")])).use(sec)
        agent.register_tool("cap10_tool", high_tool, danger=DangerLevel.HIGH)
        assert await agent.asay("x") == "CAP10:7"
        assert sandbox.run_calls > 0 and audit.record_calls > 0

        reg2 = Registry()
        default_cap = SecurityCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("sandbox") is default_cap.sandbox
        agent2 = Agent(PortArch([tool_step(
            "cap10_safe", lambda t: {"x": 1})])).use(default_cap)
        agent2.register_tool("cap10_safe", lambda x: f"CAP10-D:{x}")
        assert await agent2.asay("x") == "CAP10-D:1"
    run(scenario(), loop)


def test_cap11_tools_dual(loop: Any) -> None:
    """CAP-11 tools 双路径（asay 真消费披露索引）。

    输入：自定义 ToolRegistry + SpyDisclosure；默认 ToolsCapability。
    真实入口：ToolsCapability.install→Registry.resolve('tool'/'disclosure')；
    Agent.asay→_disclose_now→disclosure.disclose→LLM tools 实收。
    精确断言：两个 resolve 均 is 注入实例；asay 后 disclose_calls>0 且答复精确；
    默认 resolve is cap 的内部 registry/index 且 asay 正常。
    失败判据：identity 不符 / disclose 计数 0 / 答复不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        custom_registry = ToolRegistry()
        custom_registry.register(
            ToolSpec(name="cap11", description="d", danger=DangerLevel.SAFE),
            lambda **kw: "CAP11-V")
        disclosure = SpyDisclosure()
        cap = ToolsCapability(registry=custom_registry, disclosure=disclosure)
        cap.install(None, reg)
        assert reg.resolve("tool") is custom_registry
        assert reg.resolve("disclosure") is disclosure

        llm = RecordingLLM(answer="CAP11-OK")
        agent = Agent(PortArch([llm_step()])).use(ModelCapability(llm=llm)).use(cap)
        assert await agent.asay("CAP11-Q") == "CAP11-OK"
        assert disclosure.disclose_calls > 0

        reg2 = Registry()
        default_cap = ToolsCapability()
        default_cap.install(None, reg2)
        assert reg2.resolve("tool") is default_cap.registry()
        assert reg2.resolve("disclosure") is default_cap.index()
        llm2 = RecordingLLM(answer="CAP11-D")
        agent2 = Agent(PortArch([llm_step()])).use(ModelCapability(llm=llm2)).use(default_cap)
        assert await agent2.asay("CAP11-Q2") == "CAP11-D"
    run(scenario(), loop)


def test_cap12_llm_provider_dual(loop: Any) -> None:
    """CAP-12 llm_provider 双路径（工厂真构造 + 实例真消费）。

    输入：自定义工厂（计数）返回 SpyLLM；默认 openai_http 工厂。
    真实入口：LLMProviderCapability.register/resolve/install/install_into；
    Agent.asay→ModelCapability→factory 产出的 provider.complete。
    精确断言：factory 调用次数=1、resolve is 产出实例、install resolve is 实例、
    install_into 的 ModelRegistry.resolve_llm is 实例；asay 答复与 LLM 实收 messages 精确；
    默认工厂产出实例 name='openai_compat' 且可登记进 ModelRegistry。
    失败判据：计数 0 / identity 不符 / 答复不符即红。
    """
    async def scenario() -> None:
        reg = Registry()
        calls = {"n": 0}

        def factory(*, base_url: str, api_key: str, model: str) -> SpyLLM:
            calls["n"] += 1
            return SpyLLM("CAP12-PROVIDER")

        cap = LLMProviderCapability(default_factory=factory)  # type: ignore[arg-type]
        provider = cap.register("p1", base_url="https://p1.test", api_key="k", model="m")
        assert calls["n"] == 1 and cap.resolve("p1") is provider
        cap.install(None, reg)
        assert reg.resolve("llm_provider:p1") is provider
        model_reg = InMemoryModelRegistry()
        cap.install_into(model_reg)
        assert model_reg.resolve_llm("p1") is provider

        agent = Agent(PortArch([llm_step()])).use(ModelCapability(llm=provider))
        assert await agent.asay("CAP12-Q") == "CAP12-PROVIDER"
        assert provider.calls == 1 and provider.seen[0][0]["content"] == "CAP12-Q"

        default_cap = LLMProviderCapability()
        p = default_cap.register("def", base_url="https://def.test", api_key="k", model="m")
        assert p.name == "openai_compat" and default_cap.resolve("def") is p
        reg2 = InMemoryModelRegistry()
        default_cap.install_into(reg2)
        assert reg2.resolve_llm("def") is p
    run(scenario(), loop)


# =========================================================================== #
# TOOL-01~03
# =========================================================================== #
def test_tool01_schema_catalog_dual(loop: Any) -> None:
    """TOOL-01 @tool schema 真进 catalog/schema_of 双路径。

    输入：内置路径用 AddArgs(a:int,b:int) 的 @tool；自定义路径用用户 MulArgs(x:int,y:float,label:str)。
    真实入口：@tool 装饰器→ToolSpec.parameters_schema；ToolRegistry.register/catalog/schema_of。
    精确断言：L1 catalog 的 parameters_schema 为空（红线）；L2 catalog 与 schema_of 的
    properties/required 与各自 pydantic 模型字段精确一致（a/b 或 x/y/label）。
    失败判据：L1 泄漏 schema / 字段缺失 / 类型不符即红。
    """
    async def scenario() -> None:
        # 内置装饰器 + 用户最简参数模型
        @tool(name="add", danger=DangerLevel.SAFE)
        def add(args: AddArgs) -> int:
            """两数相加。"""
            return args.a + args.b

        reg = ToolRegistry()
        reg.register(add.spec, add)
        l1 = reg.catalog(level=1)
        assert l1[0].name == "add" and l1[0].description == "两数相加。"
        assert l1[0].parameters_schema == {}
        l2 = reg.catalog(level=2)
        assert l2[0].parameters_schema["properties"]["a"]["type"] == "integer"
        assert l2[0].parameters_schema["properties"]["b"]["type"] == "integer"
        assert set(reg.schema_of("add")["required"]) == {"a", "b"}

        # 用户自定义 schema 模型：字段完全不同，证明 schema 来自注入模型而非硬编码
        @tool(name="mul", danger=DangerLevel.SAFE)
        def mul(args: MulArgs) -> str:
            return f"{args.label}:{args.x * args.y}"

        reg2 = ToolRegistry()
        reg2.register(mul.spec, mul)
        schema = reg2.schema_of("mul")
        assert set(schema["properties"]) == {"x", "y", "label"}
        assert schema["properties"]["x"]["type"] == "integer"
        assert schema["properties"]["y"]["type"] == "number"
        assert schema["properties"]["label"]["type"] == "string"
        assert set(schema["required"]) == {"x", "y", "label"}
        assert reg2.catalog(level=1)[0].parameters_schema == {}
    run(scenario(), loop)


def test_tool02_call_side_effect_dual(loop: Any) -> None:
    """TOOL-02 call 真执行函数有可观察副作用双路径。

    输入：内置 ToolRegistry.call；自定义 SpyToolPort.call（用户侧 ToolPort）。
    真实入口：ToolRegistry.register/call；SpyToolPort.register/call；make_call DTO。
    精确断言：内置 result.value=45、外部 list 收到 [45]、meta.danger='SAFE'；
    自定义 call_calls=1、effects=['called:spy-add']、value='SPY-TOOL-OK'。
    失败判据：副作用 list/effects 为空或 value 不符即红。
    """
    async def scenario() -> None:
        effects: list[int] = []

        def add(a: int, b: int) -> int:
            value = a + b
            effects.append(value)
            return value

        reg = ToolRegistry()
        reg.register(ToolSpec(
            name="t02-add", description="d",
            parameters_schema={"type": "object",
                               "properties": {"a": {"type": "integer"},
                                              "b": {"type": "integer"}}},
            danger=DangerLevel.SAFE), add)
        result = await reg.call(make_call("t02-add", {"a": 20, "b": 25}), PortCtx())
        assert result.ok and result.value == 45 and effects == [45]
        assert result.meta["danger"] == "SAFE" and result.meta["source"] == "local"

        spy = SpyToolPort()
        spy.register(ToolSpec(name="spy-add", description="d",
                              danger=DangerLevel.SAFE), add)
        sr = await spy.call(make_call("spy-add", {"a": 1, "b": 2}), PortCtx())
        assert sr.value == "SPY-TOOL-OK" and spy.call_calls == 1
        assert spy.effects == ["called:spy-add"]
        assert spy.last_call.name == "spy-add"
    run(scenario(), loop)


def test_tool03_register_tool_asay_dual(loop: Any) -> None:
    """TOOL-03 register_tool 自定义工具被 Agent.asay 真调用双路径。

    输入：内置路径用普通计数函数；自定义路径用用户 ToolPort 对象的 run 方法。
    真实入口：Agent.register_tool→tool_setup→ToolRegistry；Agent.asay→tool port→函数。
    精确断言：两路径工具函数/方法 calls=1、实收参数精确、asay 答复精确。
    失败判据：计数 0 / 参数不符 / 答复不符即红。
    """
    async def scenario() -> None:
        builtin_calls: list[Any] = []

        def builtin_echo(text: str) -> str:
            builtin_calls.append(text)
            return f"TOOL03-BUILTIN:{text}"

        agent = Agent(PortArch([tool_step("t03_builtin", lambda t: {"text": t})]))
        agent.register_tool("t03_builtin", builtin_echo)
        assert await agent.asay("TOOL03-Q1") == "TOOL03-BUILTIN:TOOL03-Q1"
        assert builtin_calls == ["TOOL03-Q1"]

        class CustomToolPort:
            name = "custom_tool"

            def __init__(self) -> None:
                self.calls: list[Any] = []

            def run(self, text: str) -> str:
                self.calls.append(text)
                return f"TOOL03-CUSTOM:{text}"

        custom = CustomToolPort()
        agent2 = Agent(PortArch([tool_step("t03_custom", lambda t: {"text": t})]))
        agent2.register_tool("t03_custom", custom.run)
        assert await agent2.asay("TOOL03-Q2") == "TOOL03-CUSTOM:TOOL03-Q2"
        assert custom.calls == ["TOOL03-Q2"]
    run(scenario(), loop)


# =========================================================================== #
# STRAT-01~03
# =========================================================================== #
def test_strat01_intent_strategy_dual(loop: Any) -> None:
    """STRAT-01 IntentStrategy 经 CognitionCapability 真调用双路径。

    输入：自定义 CountingIntent；默认 CascadeIntentStrategy。
    真实入口：Agent.recognize_intent→CognitionCapability.recognize→strategy.recognize。
    精确断言：自定义 calls=1、实收 text 精确、label/confidence/gaps/trace 精确；
    默认对未命中输入 label='unknown'、confidence=0.0。
    失败判据：calls=0 / text 不符 / IntentResult 字段不符即红。
    """
    async def scenario() -> None:
        strategy = CountingIntent("STRAT01-CUSTOM")
        agent = Agent(PortArch([llm_step()])).use(
            CognitionCapability(strategy=strategy))
        out = agent.recognize_intent("STRAT01-INPUT")
        assert strategy.calls == 1 and strategy.last_text == "STRAT01-INPUT"
        assert out.label == "STRAT01-CUSTOM" and out.confidence == 0.99
        assert out.gaps == ("gap",) and out.trace == ("custom",)

        agent2 = Agent(PortArch([llm_step()])).use(CognitionCapability())
        d = agent2.recognize_intent("STRAT01-NO-MATCH")
        assert d.label == "unknown" and d.confidence == 0.0
    run(scenario(), loop)


def test_strat02_reflect_strategy_dual(loop: Any) -> None:
    """STRAT-02 ReflectStrategy 经 ReflectionCapability 真调用双路径。

    输入：自定义 CountingReflector；默认 ThresholdReflector。
    真实入口：Agent.evaluate→ReflectionCapability.critique→reflector.critique 三参真传。
    精确断言：自定义 calls=1、draft/goal 精确、Critique.score=0.42/passed=False/comments 精确；
    默认对非空草稿 passed=True/score=1.0。
    失败判据：calls=0 / 参数不符 / Critique 不符即红。
    """
    async def scenario() -> None:
        reflector = CountingReflector(0.42)
        agent = Agent(PortArch([llm_step()])).use(
            ReflectionCapability(reflector=reflector))
        out = agent.evaluate("STRAT02-DRAFT", "STRAT02-GOAL")
        assert reflector.calls == 1
        assert reflector.args[0] == "STRAT02-DRAFT" and reflector.args[1] == "STRAT02-GOAL"
        assert out.score == 0.42 and out.passed is False
        assert out.comments == "custom-critique"

        agent2 = Agent(PortArch([llm_step()])).use(ReflectionCapability())
        d = agent2.evaluate("STRAT02-ANY", "")
        assert d.passed is True and d.score == 1.0
    run(scenario(), loop)


def test_strat03_join_strategy_dual(loop: Any) -> None:
    """STRAT-03 JoinStrategy 双路径（MajorityVote 精确多数 + 自定义注入真调用）。

    输入：内置 MajorityVote；自定义 CountingJoin（作为架构 join 注入点）。
    真实入口：MajorityVote.join 直调 + JoinArch（注入 join）经 Agent.asay 真消费；
    CountingJoin 经同一 JoinArch 真消费。
    精确断言：MajorityVote 对 ['x','y','x'] 返回 'x'、平票 ['y','x','y','x'] 返回 'y'；
    内置注入 asay 答复='x'；自定义 calls=1、答复='STRAT03-CUSTOM-JOIN'。
    失败判据：多数结果不符 / 自定义计数 0 / 答复不符即红。
    """
    async def scenario() -> None:
        vote = MajorityVote()
        assert vote.join(["x", "y", "x"]) == "x"
        assert vote.join(["y", "x", "y", "x"]) == "y"

        builtin_agent = Agent(JoinArch(MajorityVote()))
        assert await builtin_agent.asay("y") == "x"

        custom = CountingJoin("STRAT03-CUSTOM-JOIN")
        custom_agent = Agent(JoinArch(custom))
        assert await custom_agent.asay("minor") == "STRAT03-CUSTOM-JOIN"
        assert custom.calls == 1
    run(scenario(), loop)
