"""架构层契约 RED 测试（用户四层契约 + 红队补漏）。

四层：
① 可插拔(R13四要素：协议+默认+入口+履约校验)
② 组合(FP 算子、10 架构复用算子、用户可封装自定义架构)
③ 门面(用户只碰语义 API say/run，不暴露 message/effect/payload/ctx)
④ 端到端(Agent(ReAct(...)).capability(...) 零胶水出结果)

RED 阶段：这些测试应因功能缺失而 FAIL（断言失败），不是 error。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import api as _api
from archs import ReAct
from compose import ToolSpec, Usage
from harness.model import ModelCapability


class _FakeLLM:
    name = "fake"
    last_usage: Usage | None = None

    async def complete(self, req: object, ctx: object) -> object:
        from compose import LLMResponse
        self.last_usage = Usage(prompt_tokens=10, completion_tokens=5, cost_usd=0.001)
        return LLMResponse(content="语义门面成功", tool_calls=[],
                           usage=self.last_usage, model="fake")

    async def stream(self, req: object, ctx: object) -> object:  # pragma: no cover
        raise NotImplementedError


class _BadStrategy:
    """不满足任何策略协议的对象（用于履约校验 RED）。"""
    pass


# ---- ③ 门面层：用户只碰语义 API ----
def test_facade_has_say_method() -> None:
    """语义门面提供 say(text)，公开签名不暴露 Effect/message/payload/ctx。"""
    Agent = getattr(_api, "Agent", None)
    assert Agent is not None, "api 应导出语义门面 Agent"
    assert hasattr(Agent, "say"), "api.Agent 应有语义方法 say()"
    sig = inspect.signature(Agent.say)
    anns = " ".join(str(p.annotation) for p in sig.parameters.values())
    for forbidden in ("Effect", "ProgramFn", "PortCtx", "payload", "message"):
        assert forbidden not in anns, f"say() 签名不应暴露 {forbidden}: {anns}"


def test_facade_has_run_method() -> None:
    """语义门面提供 run(text)（红队 S1：say/run 两个都要验）。"""
    Agent = getattr(_api, "Agent", None)
    assert Agent is not None and hasattr(Agent, "run"), "api.Agent 应有语义方法 run()"


# ---- ④ 端到端：零胶水出结果 ----
def test_zero_glue_say_returns_result() -> None:
    """一条链出结果：Agent(ReAct).capability(...).say(text) 直接拿到文本，非 Session。"""
    Agent = getattr(_api, "Agent", None)
    assert Agent is not None, "api 应导出语义门面 Agent"
    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    agent.capability(ModelCapability(llm=_FakeLLM()))
    out = agent.say("你好")  # 语义门面是同步 API，内部完成异步执行
    assert out == "语义门面成功", f"say() 应直接出结果文本，实际 {out!r}"


# ---- ② 组合层：10 架构复用算子 ----
def test_all_ten_archs_compile_reuse_operators() -> None:
    """10/10 内置架构 compile 复用组合算子（非手搓控制流），AC-7。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "archs" / "recipes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    arch_names = {"ToolUse", "ReAct", "PlanExecute", "Reflection", "Router",
                  "RouterSkill", "Supervisor", "Handoff", "Blackboard", "Graph"}
    ops = {"seq", "par", "branch_value", "loop_until", "map_effect", "fallback", "gate", "retry"}
    missing: list[str] = []
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in arch_names):
        compiles = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "compile"]
        assert compiles, f"{cls.name} 无 compile"
        called = {n.func.id for n in ast.walk(compiles[0])
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if not (called & ops):
            missing.append(f"{cls.name}: {called}")
    assert not missing, f"这些架构未复用组合算子: {missing}"


def test_user_can_build_custom_arch_with_fp_operators() -> None:
    """用户用 compose FP 算子封装自定义架构，与内置架构同权（红队 B3 方案 A）。

    Effect 是组合单元对用户公开；只有 message/payload 数据由门面注入。
    """
    dsl = getattr(_api, "dsl", None)
    assert dsl is not None, "api 应导出 FP 组合入口 dsl"
    from runtime import Invoke, Terminate

    def my_program():
        out = yield Invoke(port="llm", payload={"prompt": "hi"})
        yield Terminate(output=out)

    # 用户用 FP 算子组合 + custom_arch 封装，与内置 Arch 同形
    custom = dsl.custom_arch(dsl.seq(my_program, my_program), name="my-arch")
    assert custom.name == "my-arch"
    assert custom.static_check().ok, "自定义架构应与内置一样可 static_check"
    assert callable(custom.compile), "自定义架构应可 compile 成 Program"


# ---- ① 可插拔层：履约校验（红队 B4 四要素之校验）----
def test_invalid_capability_rejected_at_assembly() -> None:
    """注入非法策略对象，装配期履约校验拒绝（isinstance Protocol）。"""
    Agent = getattr(_api, "Agent", None)
    assert Agent is not None
    agent = Agent(ReAct([ToolSpec(name="t", description="t")], llm="fake", max_turns=2))
    with pytest.raises((TypeError, ValueError)):
        agent.inject_strategy(_BadStrategy())  # 非法实现应被拒
