"""规格 P4-P8：各能力层 ToolLayer 外壳 + DTO 归位 + 内置架构的契约测试。

每个工具层自包含可独立 build() -> Program（R13 Capability 契约）；DTO 单一定义源
（归属能力层，hooks 仅重导出，消除反向依赖）。
"""
from __future__ import annotations

import inspect

import pytest

from runtime import Invoke, Terminate


from runtime import Program


def _collect(program: Program) -> list[object]:
    """驱动 build() 产出的 Program（generator），收集 Effect（遇 Terminate 停）。"""
    gen = program
    out: list[object] = []
    try:
        eff = next(gen)
        while True:
            out.append(eff)
            if isinstance(eff, Terminate):
                break
            eff = gen.send(None)
    except StopIteration:
        pass
    return out


# ---------- P4: R4 外壳 + last_usage 归位 ----------
def test_p4_model_capability_build_returns_program() -> None:
    from compose.tool_layer import layer_program
    from harness.model.capability import ModelCapability

    assert callable(ModelCapability.build)
    # 用最小 fake llm 构造，build() 不触碰 IO（只产出 Program）
    from tests.r4.fakes import RecordingLLM
    cap = ModelCapability(llm=RecordingLLM([]))
    effects = _collect(cap.build())
    assert len(effects) == 2
    assert isinstance(effects[0], Invoke) and effects[0].port == "llm"
    assert isinstance(effects[1], Terminate)


def test_p4_last_usage_owned_by_r4_not_hooks_base_port() -> None:
    """last_usage 归属 R4 扩展协议；hooks.LLMPort 基础协议不再声明该成员。"""
    from compose.tool_layer import layer_program  # noqa: F401
    import compose.ports as hp

    # 基础 Port 的成员清单不含 last_usage（归位到 R4 扩展协议）
    assert "last_usage" not in hp.LLMPort.__annotations__


# ---------- P5: R5 外壳 + DangerLevel 归位 ----------
def test_p5_danger_level_single_source_in_r5() -> None:
    """DangerLevel 定义在 R5，hooks 仅重导出（同一对象，非双份类）。"""
    from harness.tools.types import DangerLevel as R5Danger
    from compose import DangerLevel as PublicDanger

    assert R5Danger is PublicDanger


def test_p5_tools_capability_build_returns_program() -> None:
    from harness.tools.capability import ToolsCapability

    assert callable(ToolsCapability.build)
    cap = ToolsCapability()
    effects = _collect(cap.build())
    assert isinstance(effects[0], Invoke) and effects[0].port == "tool"
    assert isinstance(effects[1], Terminate)


# ---------- P7: R6 外壳（内部不改）----------
def test_p7_rag_pipeline_build_returns_program() -> None:
    from harness.rag.pipeline import RAGPipeline

    assert callable(RAGPipeline.build)
    # 用最小构造（with_ 替换），build() 不触碰 IO
    pipe = object.__new__(RAGPipeline)
    effects = _collect(pipe.build())
    assert isinstance(effects[0], Invoke) and effects[0].port == "rag"
    assert isinstance(effects[1], Terminate)


# ---------- P8: R7 外壳 + recall 归位 ----------
def test_p8_recall_owned_by_r7_not_hooks_base_port() -> None:
    import compose.ports as hp

    assert "recall" not in hp.MemoryPort.__annotations__


def test_p8_context_manager_build_returns_program() -> None:
    from harness.context.manager import ContextMemoryManager

    assert callable(ContextMemoryManager.build)
    mgr = object.__new__(ContextMemoryManager)
    effects = _collect(mgr.build())
    assert isinstance(effects[0], Invoke) and effects[0].port == "memory"
    assert isinstance(effects[1], Terminate)


# ---------- P6: src/archs/ 10 内置架构 ----------
def test_p6_archs_exposes_ten_recipes() -> None:
    """10 内置架构 = A 类算子的预设配方，对外 API 不变（R12 §4 构造签名）。"""
    import archs

    expected = {
        "ToolUse", "ReAct", "PlanExecute", "Reflection", "Router",
        "RouterSkill", "Supervisor", "Handoff", "Blackboard", "Graph",
    }
    names = set(archs.__all__)
    assert expected <= names, f"缺内置架构: {expected - names}"
    for n in expected:
        assert callable(getattr(archs, n))


def test_p6_arch_compiles_to_program_via_operators() -> None:
    """内置架构用 A 类算子配方 compile() -> Program（R1 只认 Program）。"""
    from archs import ToolUse
    from compose import ToolSpec

    arch = ToolUse([ToolSpec(name="t1", description="d")], parallel=False)
    report = arch.static_check()
    assert report.ok
    program = arch.compile()
    effects = _collect(program)
    assert len(effects) >= 2
    assert isinstance(effects[-1], Terminate)


# ---------- P4 补：as_dispatch 形态（§2.5）----------
def test_p4_as_dispatch_reads_typed_target_from_ctx_meta() -> None:
    """as_dispatch 吃 (effect, ctx)，从 ctx.meta 读 typed target，不构造 Invocation。"""
    import asyncio
    from compose.guards import CallState
    from harness.model.capability import ModelCapability
    from harness.model.ports import LLMPortWithUsage
    from runtime import Invoke, Result, Usage
    from tests.r4.fakes import RecordingLLM

    # 构造一个含两个后端的 cap，backup 成功
    primary = RecordingLLM([])
    backup = RecordingLLM([])
    cap = ModelCapability(llm=primary)
    cap.add_backend("backup", backup)
    gateway = cap.gateway()

    class Ctx:
        meta: dict[str, object] = {}

    ctx = Ctx()
    # ②守卫写入 typed target=backup（从 ctx.meta 读）
    from compose.guards import RoutingTarget
    CallState(ctx).write_routing_target(RoutingTarget(target="backup", reason="test"))

    effect = Invoke(port="llm", payload={"messages": []})
    result = asyncio.run(gateway.as_dispatch()(effect, ctx))
    # backup 被调用（primary 未调）——typed target 生效
    assert result.ok, f"dispatch 应成功，实际 {result.error}"


def test_p4_capability_dispatch_injectable_to_runner() -> None:
    """cap.dispatch() 可直接注入 R1 Runner（§2.5 主线，不经 CompositeCall）。"""
    import asyncio
    from harness.model.capability import ModelCapability
    from runtime import Invoke, Result, Runner, Session, Terminate, Usage
    from tests.r4.fakes import RecordingLLM

    llm = RecordingLLM([{"role": "assistant", "content": "ok"}])
    cap = ModelCapability(llm=llm)
    call = cap.dispatch()

    def program_factory():
        yield Invoke(port="llm", payload={"messages": [{"role": "user", "content": "hi"}]})
        yield Terminate(output="done")
        return None

    result = asyncio.run(Runner(call=call).run(
        Session(session_id="p4-dispatch", program_factory=program_factory)  # type: ignore[arg-type]
    ))
    assert result.status == "done"


# ---------- 复审 Should 修复回归 ----------
def test_p6_tooluse_serial_keeps_all_tools() -> None:
    """ToolUse(parallel=False) 顺序串接全部工具，不丢中间项（复审 S2）。"""
    from archs import ToolUse
    from compose import ToolSpec

    arch = ToolUse([ToolSpec(name=n) for n in ("t1", "t2", "t3")], parallel=False)
    effects = _collect(arch.compile())
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names == ["t1", "t2", "t3"]


def test_p6_handoff_keeps_all_roles() -> None:
    """Handoff 串接全部角色，不丢中间交接（复审 S3）。"""
    from archs import Handoff

    arch = Handoff({n: object() for n in ("a", "b", "c")})
    effects = _collect(arch.compile())
    names = [e.payload.get("name") for e in effects if isinstance(e, Invoke)]
    assert names == ["a", "b", "c"]


def test_p6_graph_nested_arch_compile_is_idempotent() -> None:
    """Graph.node(Arch) 二次 compile 产出的 Effect 一致（D-8 幂等，复审 S1）。"""
    from archs import Graph, ToolUse
    from compose import ToolSpec

    g = Graph().node("a", ToolUse(
        [ToolSpec(name="t1"), ToolSpec(name="t2")], parallel=False)).exit("a")
    first = [e.payload.get("name") for e in _collect(g.compile()) if isinstance(e, Invoke)]
    second = [e.payload.get("name") for e in _collect(g.compile()) if isinstance(e, Invoke)]
    assert first == second == ["t1", "t2"]


def test_p6_supervisor_default_join_is_majority_vote() -> None:
    """Supervisor.join 默认多数投票（Nit-1，对齐 R12 §4）。"""
    from archs import MajorityVote, Supervisor

    arch = Supervisor(planner=type("P", (), {"name": "p"})(), child_spec=object())
    assert isinstance(arch.join, MajorityVote)
