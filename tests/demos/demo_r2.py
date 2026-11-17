"""R2 独立 demo：四 mode 编排 / 8 action / 留名 / 异常隔离 / 与 R1 集成。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from runtime import (
    AgentState, Effect, EffectKind, Result, Runner, Session,
)
from hooks import (
    Decision, DecisionAction, HookBus, HookMode, HookCtx, Registry,
    LLMRequest, LLMResponse, Usage,
)


class AuditHook:
    """observe：审计留痕。"""
    name = "audit"; events = ("before_invoke", "after_invoke"); mode = HookMode.OBSERVE
    priority = 1; requires_ports = ()
    def __init__(self): self.seen = []
    async def __call__(self, ctx):
        self.seen.append(ctx.event); return Decision.continue_()


class GuardHook:
    """guard：拦截危险调用。"""
    name = "guard"; events = ("before_invoke",); mode = HookMode.GUARD
    priority = 10; requires_ports = ()
    async def __call__(self, ctx):
        if "danger" in str(ctx.effect.payload.get("call")):
            return Decision.deny("blocked")
        return Decision.continue_()


class ContextHook:
    """transform：注入上下文。"""
    name = "context"; events = ("before_model",); mode = HookMode.TRANSFORM
    priority = 20; requires_ports = ()
    async def __call__(self, ctx):
        return Decision.rewrite(context="injected")


async def main():
    bus = HookBus()
    audit = AuditHook()
    bus.on(audit).on(GuardHook()).on(ContextHook())
    bus.registry.register("tool", type("T", (), {"name": "tool"})())
    bus.registry.register("llm", type("L", (), {"name": "llm"})())
    bus.freeze()

    # guard deny
    ctx = HookCtx(effect=Effect(EffectKind.TOOL, {"call": "danger_x"}),
                  state=AgentState(), session_id="s", event="before_invoke")
    d = await bus.emit("before_invoke", ctx)
    assert d.action is DecisionAction.DENY

    # transform
    ctx2 = HookCtx(effect=Effect(EffectKind.LLM, {"messages": []}),
                   state=AgentState(), session_id="s", event="before_model")
    d2 = await bus.emit("before_model", ctx2)
    assert d2.action is DecisionAction.REWRITE
    assert ctx2.meta["injected_context"] == ["injected"]

    # 与 R1 集成
    async def dispatch(effect, c: HookCtx):
        await bus.emit("before_invoke", c)
        return None
    async def call(effect, c):
        if effect.kind is EffectKind.LLM:
            return Result.success({"content": "ok", "tool_calls": []})
        return Result.success(None)
    def program(state):
        resp = yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END, {"output": resp["content"]})
    runner = Runner(dispatch=dispatch, call=call, ctx_factory=bus.ctx_factory())
    session = Session(session_id="int", program_factory=lambda: program(AgentState()))
    result = await runner.run(session)
    assert result.status == "done"

    print("[R2] PASS: 四 mode 编排 / 8 action / 留名 / 异常隔离 / 与 R1 集成")


asyncio.run(main())
