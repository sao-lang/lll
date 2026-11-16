"""M4 独立闭环 demo：@tool / static_check / 三级披露 / MCP 注入 / danger 四级路由 / 异常折叠 / 集成。

运行：make demo-m4
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from chains import Chains, MountPoint  # noqa: E402
from chains.builtins import AllowFilter  # noqa: E402
from harness.tools import (  # noqa: E402
    DangerLevel, ToolDefinitionError, ToolIndex, ToolRegistry, tool,
)
from harness.tools.fakes import (  # noqa: E402
    DictSkillProvider, FakeMCPTransport, FakeSandbox, StaticApprover,
)
from harness.tools.mcp import MCPManager  # noqa: E402
from ports import (  # noqa: E402
    PortCtx, PortRegistry, ToolCall, ToolSpec, make_call,
)
from runtime import AgentState, Effect, EffectKind, Runner, Session, canonical  # noqa: E402


class Meta(BaseModel):
    locale: str = "zh-CN"


class EchoArgs(BaseModel):
    text: str = Field(..., description="要回显的文本")
    times: int = Field(1, gt=0, le=5)
    meta: Meta | None = None


class TransferArgs(BaseModel):
    amount: float = Field(..., gt=0, description="转账金额")


@tool(name="echo", danger=DangerLevel.SAFE)
def echo(args: EchoArgs, ctx: PortCtx):
    """回显文本。"""
    return args.text * args.times


@tool(name="write_tmp", danger=DangerLevel.LOW)
def write_tmp(args: EchoArgs, ctx: PortCtx):
    return f"written:{args.text}"


@tool(name="exec_code", danger=DangerLevel.HIGH)
def exec_code(args: EchoArgs, ctx: PortCtx):
    return "executed"


@tool(name="transfer", danger=DangerLevel.CRITICAL)
def transfer(args: TransferArgs, ctx: PortCtx):
    return f"transferred:{args.amount}"


@tool(name="boom", danger=DangerLevel.SAFE)
def boom(args: EchoArgs, ctx: PortCtx):
    raise RuntimeError("tool exploded")


async def main() -> None:
    sandbox = FakeSandbox()
    approver = StaticApprover(True)
    registry = ToolRegistry(sandbox=sandbox, approver=approver)
    for fn in (echo, write_tmp, exec_code, transfer, boom):
        registry.add(fn.__agentkit_tool__)

    # ① @tool → pydantic JSON Schema（含 gt / 枚举 / 嵌套 / extra=forbid）
    schema = registry.schema_of("echo")
    props = schema["properties"]
    assert props["times"]["exclusiveMinimum"] == 0 and props["times"]["maximum"] == 5
    assert "$defs" in schema and schema["additionalProperties"] is False
    assert "locale" in schema["$defs"]["Meta"]["properties"]
    print("[M4] @tool → JSON Schema 含 gt/le/嵌套/extra=forbid，docstring → description")

    # ② 装配期 static_check + 红线
    assert registry.static_check() == []
    registry.freeze()
    try:
        registry.register(ToolSpec(name="no-danger"), lambda a, c: None)
        raise AssertionError("应拒绝未声明 danger")
    except ToolDefinitionError:
        print("[M4] static_check 通过；未声明 danger → 注册期拒绝")

    # ③ danger 四级路由 + 异常折叠
    results = {}
    for name, args in (
        ("echo", {"text": "hi", "times": 2}),
        ("write_tmp", {"text": "a"}),
        ("exec_code", {"text": "x"}),
        ("transfer", {"amount": 42.0}),
        ("boom", {"text": "x"}),
    ):
        results[name] = await registry.call(ToolCall(call_id=name, name=name, args=args), PortCtx("demo-m4"))
    assert results["echo"].value == "hihi" and not results["echo"].meta["sandbox"]
    assert results["write_tmp"].meta["sandbox"] and results["exec_code"].meta["sandbox"]
    assert results["transfer"].meta["approved"] and results["transfer"].meta["sandbox"]
    assert not results["boom"].ok and "RuntimeError" in results["boom"].error
    bad = await registry.call(ToolCall(call_id="x", name="echo", args={"text": "a", "bad": 1}), PortCtx("demo-m4"))
    assert not bad.ok and "unexpected arguments" in bad.error
    print(f"[M4] 四级路由 → SAFE 直执行 / LOW,HIGH 沙盒 / CRITICAL 审批；沙盒调用 {len(sandbox.runs)} 次")
    print("[M4] 工具异常折叠为 ToolResult(ok=False)，未抛穿")

    # ④ MCP 注入：命名空间 + 同一 Registry + 只注入 L1 目录
    manager = MCPManager(registry, FakeMCPTransport())
    injected = await manager.connect("demo")
    assert [s.name for s in injected] == ["mcp__demo__add", "mcp__demo__weather"]
    assert all(s.parameters_schema == {} for s in registry.catalog(level=1))
    remote = await manager.call("mcp__demo__add", {"a": 2, "b": 3}, PortCtx("demo-m4"))
    assert remote.ok and remote.value == 5 and remote.meta["source"] == "mcp"
    assert (await manager.health()) == {"demo": True}
    print(f"[M4] MCP 注入 → {len(injected)} 工具，L1 仅目录，调用结果={remote.value}")

    leak = ToolRegistry(index_enabled=False, sandbox=sandbox, approver=approver)
    await MCPManager(leak, FakeMCPTransport()).connect("demo")
    assert any("MCP tools fully injected" in e for e in leak.static_check())
    print("[M4] 红线 → MCP 工具全量注入上下文被 static_check 拒绝")

    # ⑤ 三级渐进式披露（25 个工具场景）
    bulk = ToolRegistry()
    for i in range(25):
        bulk.register(ToolSpec(
            name=f"t{i}", description=f"工具 {i}",
            parameters_schema={"type": "object", "properties": {
                f"param_{j}": {"type": "string", "description": f"参数 {j} 的详细说明与约束"}
                for j in range(8)
            }},
            danger=DangerLevel.SAFE,
        ), lambda args, c: "ok")
    index = ToolIndex(bulk, DictSkillProvider(), threshold=20)
    assert index.should_index()
    l1 = index.catalog()
    full = canonical(bulk.catalog(level=2))
    assert len(canonical(l1)) < len(full) / 3
    l2 = index.load_schema(["t0"])
    l3 = index.load_body("python-expert")
    assert "param_0" in l2[0]["parameters_schema"]["properties"] and l3.startswith("你是一名")
    assert {e.level for e in index.entries} == {1, 2, 3}
    print(f"[M4] 披露 → L1 {len(canonical(l1))} chars vs 全量 {len(full)} chars；L2/L3 按需加载")

    # ⑥ 全链路集成：M1 Runner + M2 链路 + M3 call + M4 Registry（ToolUse 架构）
    chain = Chains().filter(AllowFilter("tool-guard"), at=MountPoint.BEFORE_TOOL).freeze(("tool",))
    ports = PortRegistry().register("tool", registry)
    ports.freeze(("tool",))

    def tool_use(state: AgentState):
        batch = yield Effect(EffectKind.TOOL, {
            "calls": [
                {"call_id": "c1", "name": "echo", "args": {"text": "A"}},
                {"call_id": "c2", "name": "mcp__demo__add", "args": {"a": 1, "b": 4}},
            ],
        }, batch=True)
        yield Effect(EffectKind.END, {"output": [r.value for r in batch]})

    runner = Runner(dispatch=chain.dispatch, call=make_call(ports), ctx_factory=chain.ctx_factory())
    final = await runner.run(Session("m4-tooluse", lambda: tool_use(AgentState())))
    assert final.status == "done" and final.output == ["A", 5]
    print(f"[M4+M1+M2+M3] ToolUse 批量工具 → {final.output}")

    print("[M4] PASS: @tool / schema / static_check / 四级路由 / MCP / 披露 / 异常折叠 / 集成")


if __name__ == "__main__":
    asyncio.run(main())
