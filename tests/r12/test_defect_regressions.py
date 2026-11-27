"""缺陷回归（IronForge 实测 #1/#2，2026-11-28 修复）：

#1 ModelCapability.install 被门面以 compose Registry 注入时不得覆盖 ModelRegistry
   （原实现覆盖后，显式路由 resolve_llm 必然 AttributeError）；
#2 ReAct 对 LLM 调用失败（runner 回注 Result 对象本身）必须 fail-closed 崩出
   （status=error），不得 _dict 吞成空结论正常终止（原实现 output=None, done）。
"""
from __future__ import annotations

import asyncio

from compose import DangerLevel, Registry, ToolSpec
from harness.model import InMemoryModelRegistry, ModelCapability
from ports.adapters.llm import OpenAICompatLLM
from runtime import Result, Runner, Session
from archs import ReAct


def test_install_with_plain_registry_preserves_model_registry() -> None:
    """#1：门面形态注入 compose Registry → ModelRegistry 保持权威，双后端可解析。"""
    llm = OpenAICompatLLM(base_url="https://api.example/v1", api_key="sk-t", model="m")
    registry = InMemoryModelRegistry()
    registry.register_llm("primary", llm)
    registry.register_llm("fast", llm)
    cap = ModelCapability(llm=llm, registry=registry)

    class _Container:
        registry: object | None = None

    container = _Container()
    returned = cap.install(container, Registry())  # 门面形态：compose Registry

    assert returned is registry, "install 必须返回模型域注册表（不得被 compose Registry 覆盖）"
    assert container.registry is registry
    assert cap.registry.resolve_llm("primary") is llm
    assert cap.registry.resolve_llm("fast") is llm


def test_install_with_model_registry_takes_over() -> None:
    """注入方本身具备 resolve_llm（模型域注册表）→ 保持原接管语义。"""
    llm = OpenAICompatLLM(base_url="https://api.example/v1", api_key="sk-t", model="m")
    takeover = InMemoryModelRegistry()
    takeover.register_llm("primary", llm)
    cap = ModelCapability(llm=llm, registry=InMemoryModelRegistry())

    returned = cap.install(None, takeover)  # type: ignore[arg-type]
    assert returned is takeover
    assert cap.registry is takeover


def _run(arch, *, call) -> tuple[str, object, str]:
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="r12-defect", program_factory=lambda: arch.compile())))
    return res.status, res.output, res.reason


def test_react_fails_closed_on_failed_llm_backfill() -> None:
    """#2：LLM 调用失败（回注 Result 对象）→ status=error，非空结论终止。"""
    spec = ToolSpec(name="noop", description="no-op", danger=DangerLevel.SAFE)
    arch = ReAct([spec], llm="primary", max_turns=3)

    async def failing_call(effect: object, ctx: object) -> Result:
        _ = effect, ctx
        return Result.failure("PORT_CRASHED", "模拟后端故障")

    status, output, reason = _run(arch, call=failing_call)
    assert status == "error", f"必须 fail-closed（status={status}, output={output!r}）"
    assert "拒绝静默终止" in reason


def test_react_still_terminates_on_healthy_backfill() -> None:
    """修复不改变健康路径：有 content 无 tool_calls → 正常终止 output=content。"""
    spec = ToolSpec(name="noop", description="no-op", danger=DangerLevel.SAFE)
    arch = ReAct([spec], llm="primary", max_turns=3)

    async def healthy_call(effect: object, ctx: object) -> Result:
        _ = effect, ctx
        return Result.success({"content": "结论文本", "tool_calls": []})

    status, output, reason = _run(arch, call=healthy_call)
    assert status == "done"
    assert output == "结论文本"


def test_react_tool_calls_still_dispatch() -> None:
    """修复不影响工具轮：回注 tool_calls → 工具被真实调用（接线语义回归）。"""
    spec = ToolSpec(name="echo", description="echo", danger=DangerLevel.SAFE)
    arch = ReAct([spec], llm="primary", max_turns=3)
    seen: list[str] = []

    async def call(effect: object, ctx: object) -> Result:
        _ = ctx
        port = getattr(effect, "port", "")
        if port == "llm":
            if seen:
                return Result.success({"content": "完成", "tool_calls": []})
            # 门面 _call_llm 已把 ToolCall 归一为 dict（真实管线形态）
            return Result.success({"content": None, "tool_calls": [
                {"call_id": "c1", "name": "echo", "args": {}}]})
        if port == "tool":
            # 解释器已把 batch 展开为独立 Invoke：payload 携带 "call"（单条）
            call_item = getattr(effect, "payload", {}).get("call")
            if isinstance(call_item, dict):
                seen.append(str(call_item.get("name", "")))
            return Result.success("echoed")
        return Result.failure("PORT_MISSING", f"unexpected port {port}")

    res = asyncio.run(Runner(call=call).run(
        Session(session_id="r12-defect-tool", program_factory=lambda: arch.compile())))
    assert res.status == "done"
    assert seen == ["echo"], "tool_calls 必须仍被派发（回归保护）"
    assert res.output == "完成"
