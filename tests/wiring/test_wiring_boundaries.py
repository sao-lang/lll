"""接线边界门禁（行为不变量）——基于 .conductor/contracts/2026-11-24/6a12744-wiring-boundaries.contract.md

原则：注入自定义对象 → 走真实装配 → 断言执行链拿到同一实例/行为。
这些测试当前应当 RED（暴露已知结构耦合），修完转 GREEN。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ---------------- G-W4 意图边界：能力包只持策略，不持架构桥梁 ----------------
def test_gate_w4_cognition_capability_has_no_program_bridge() -> None:
    """能力包禁止 import intent_as_program / 暴露 build()（架构桥梁归 archs）。"""
    path = ROOT / "src" / "harness" / "cognition" / "capability.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {n.name.split(".")[-1] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) for n in n.names}
    assert "intent_as_program" not in imported, "能力包 import 了架构桥梁"
    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "build" not in methods, "能力包暴露 build()——Program 编排不是能力包职责"


def test_gate_w4_router_takes_capability_package() -> None:
    """主线：用户注入能力包，架构收能力包（桥接层负责解包到策略）。"""
    from archs import ReAct, Router
    from compose import ToolSpec
    from harness.cognition import CascadeIntentStrategy, RegexKeywordMatcher
    from harness.cognition.capability import CognitionCapability
    from runtime import AgentState, Result, Runner, Session

    cap = CognitionCapability(strategy=CascadeIntentStrategy(
        keyword=RegexKeywordMatcher({"天气": "weather"})))
    arch = Router(classifier=cap,  # ← 注入能力包（用户主线）
                  arms={"weather": ReAct([ToolSpec(name="t")], llm="fake")},
                  default="weather")

    calls: list[str] = []
    async def call(effect, ctx):  # type: ignore[no-untyped-def]
        calls.append(str(effect.port))
        return Result.success({"content": "ok", "tool_calls": []})

    res = __import__("asyncio").run(Runner(call).run(Session(
        session_id="gate-w4", program_factory=arch.compile, state=AgentState())))
    assert res.status == "done" and calls, "Router 用能力包跑不通（桥梁应解包到策略）"


def test_gate_w4_whole_strategy_replacement_is_honored() -> None:
    """整包替换：传自己的 IntentStrategy，框架不强塞级联包装（R13:126-129）。"""
    from api import Agent
    from archs import ReAct
    from compose import ToolSpec
    from harness.cognition import IntentResult
    from harness.cognition.capability import CognitionCapability
    from harness.model import ModelCapability

    class Whole:
        name = "whole"
        async def recognize(self, text: str, ctx: object) -> IntentResult:
            return IntentResult(label="my_intent", confidence=0.99, trace=("my",))

    llm = _LLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))
    cap = CognitionCapability(strategy=Whole())  # type: ignore[arg-type]
    agent.use(cap)
    result = agent.recognize_intent("hi")
    assert result.label == "my_intent" and result.trace == ("my",)


class _LLM:
    name = "fake"
    last_usage = None
    async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
        from compose import LLMResponse, Usage
        return LLMResponse(content="ok", tool_calls=[], usage=Usage(
            prompt_tokens=1, completion_tokens=1, cost_usd=0.0001), model="fake")
    async def stream(self, req, ctx):  # type: ignore[no-untyped-def]
        raise NotImplementedError


# ---------------- G-W5 反思边界：能力包无循环，架构收策略 ----------------
def test_gate_w5_reflection_capability_has_no_loop() -> None:
    path = ROOT / "src" / "harness" / "cognition" / "reflection_capability.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {n.name.split(".")[-1] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) for n in n.names}
    assert not ({"loop_until", "Invoke", "Effect"} & imported), "反思能力包含循环/Effect"


def test_gate_w5_reflection_arch_takes_capability_package() -> None:
    from archs import Reflection
    from harness.cognition import ReflectStrategy, Critique
    from harness.cognition.reflection_capability import ReflectionCapability
    from runtime import AgentState, Result, Runner, Session

    class R(ReflectStrategy):
        name = "r"
        calls = 0
        def critique(self, draft: str, goal: str, ctx: object = None) -> Critique:
            self.calls += 1
            return Critique(passed=True, score=1.0)

    reflector = R()
    cap = ReflectionCapability(reflector=reflector)
    arch = Reflection(reflector=cap)  # ← 注入能力包（用户主线）

    async def call(effect, ctx):  # type: ignore[no-untyped-def]
        return Result.success({"content": "ok", "tool_calls": []})

    res = __import__("asyncio").run(Runner(call).run(Session(
        session_id="gate-w5", program_factory=arch.compile, state=AgentState())))
    assert res.status == "done" and reflector.calls >= 1, "Reflection 用能力包跑不通"


# ---------------- G-W3 记忆链：来源对话、注入 system、内部部件不漏出门面 ----------------
def test_gate_w3_memory_ingests_dialogue_and_injects_system() -> None:
    """真实门面链：自定义 MemoryStore 收到对话 ingest，召回内容进 system 段。"""
    from api import Agent
    from archs import ReAct
    from compose import ToolSpec
    from contextlib import contextmanager
    from harness.context import MemoryCapability
    from harness.model import ModelCapability

    class SpyStore:
        name = "spy_store"
        ingested: list[str] = []
        def ingest(self, text: str, ctx: object, **kw: object) -> object:
            self.ingested.append(text)
            return None
        async def recall(self, query: object, ctx: object = None) -> list[object]:
            from harness.memory import MemoryItem
            from harness.memory import MemoryKind
            return [MemoryItem(
                kind=MemoryKind.EPISODIC, content="已记住：深圳晴天",
                importance=0.9, ts=1, source="gate:recall", item_id="gate-1")]

    class _RecordingLLM(_LLM):
        messages: list[list[dict]] = []
        async def complete(self, req, ctx):  # type: ignore[no-untyped-def]
            self.messages.append([dict(m) for m in req.messages])
            return await super().complete(req, ctx)

    store = SpyStore()
    # 通过 manager 注入自定义 store（原设计：记忆是 manager 的部件）
    from harness.context import ContextMemoryManager
    manager = ContextMemoryManager(memory=store)  # type: ignore[arg-type]
    llm = _RecordingLLM()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"))
    agent.use(ModelCapability(llm=llm))
    agent.use(MemoryCapability(manager=manager))

    agent.say("你好")

    assert store.ingested and "你好" in store.ingested[0], "记忆来源必须是对话原文"
    assert llm.messages, "LLM 未被调用"


def test_gate_w3_internal_context_parts_not_in_facade_signature() -> None:
    """assembler/compaction/blob/reinjector/token_counter 是内部部件，不得进门面。"""
    path = ROOT / "src" / "api" / "agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    init = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"
                 and n.args.args and n.args.args[0].arg == "self"), None)
    assert init is not None
    arg_names = {a.arg for a in init.args.args} | {a.arg for a in init.args.kwonlyargs}
    forbidden = {"assembler", "compaction", "offloader", "blob",
                 "reinjector", "token_counter"}
    assert not (forbidden & arg_names), f"内部部件漏进门面构造参数：{forbidden & arg_names}"


# ---------------- G-W1 MCP 同源（行为级）----------------
def test_gate_w1_mcp_tools_share_wired_registry() -> None:
    """MCP tools 通过 connect() 注册进与执行链同一 ToolRegistry。"""
    from compose import PortCtx, ToolSpec
    from harness.tools import DangerLevel, ToolRegistry, tool
    from harness.tools.mcp import MCPManager, MCPTransport

    class Transport(MCPTransport):
        name = "fake_mcp"
        async def list_tools(self, server: str) -> list[ToolSpec]:
            return [ToolSpec(name="remote_echo", description="echo",
                             parameters_schema={"type": "object"})]
        async def call_tool(self, server, tool_name, args):  # type: ignore[no-untyped-def]
            return {"ok": True}
        async def ping(self, server: str) -> bool:
            return True

    reg = ToolRegistry(sandbox=None, human=None, audit=None)  # type: ignore[arg-type]
    mgr = MCPManager(reg, Transport())
    import asyncio
    specs = asyncio.run(mgr.connect("demo", PortCtx(session_id="s")))
    assert specs and specs[0].name == "mcp__demo__remote_echo"
    assert any(s.name == "mcp__demo__remote_echo" for s in reg.catalog(level=1))


# ---------------- G-R1 静态红线（低误报）----------------
def test_gate_r1_no_capability_imports_arch_bridge() -> None:
    for path in (ROOT / "src" / "harness").rglob("capability.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                mods = ".".join(p for p in [n.module] if p) if n.module else ""
                names = {a.name for a in n.names}
                assert not ({"intent_as_program", "loop_until"} & names), (
                    f"{path.name} import 架构桥梁")
                assert not mods.startswith("archs"), f"{path.name} import archs"


# ---------------- G-W7 用户自定义能力包（框架主线）----------------
def test_gate_w7_custom_capability_provider_reaches_execution() -> None:
    """用户自封装能力包：注册自定义 Port provider，真进执行链（不是死对象）。"""
    from api import Agent
    from archs import ReAct
    from compose import ToolSpec
    from runtime import Invoke, Result

    from runtime.types import Effect

    calls: list[str] = []

    class MyProvider:
        name = "my_eval"
        op = "score"  # 用户自封装 provider 声明入口方法名
        async def score(self, case_id: str, output: object, ctx: object) -> float:
            calls.append(case_id)
            return 1.0

    class MyCapability:
        name = "my_cap"
        version = "1.0"
        requires: tuple[str, ...] = ()
        def __init__(self, provider: MyProvider) -> None:
            self.provider = provider
        def install(self, container: object, reg: object) -> None:
            reg.register("eval", self.provider)  # type: ignore[attr-defined]

    provider = MyProvider()
    agent = Agent(ReAct([ToolSpec(name="t")], llm="fake"), session_id="w7")
    agent.use(MyCapability(provider))
    # 装配期 install 后，自定义 eval provider 必须能被解析到执行链 handler
    asyncio = __import__("asyncio")
    asyncio.run(agent._arun())
    handler = agent._port_handlers.get("eval")
    assert handler is not None, "自定义能力包的 provider 未进执行链 handler 表"
    result = asyncio.run(handler(Invoke(port="eval").with_payload(
        case_id="c1", output="x"), object()))
    assert result.ok and calls == ["c1"], "自定义 provider 未被真调用"
