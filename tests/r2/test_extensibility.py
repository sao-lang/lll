"""R2 插件能力回归：Decision 各 action 真实生效 + around 改写 effect 且 Port 只调一次。

历史 bug（本测试防复发）：
1. R1 用 getattr(outcome,'abort',False) 判定，把 Decision.abort 类方法当真值 → 所有 hook 被误判 abort。
2. around 中心 invoke 后 R1 又用原始 effect 重复调 Port → 插件改写失效 + 副作用执行两次。
3. 裁决载体字段 R1 用 data、Decision 实际是 value → 不一致。
"""
import asyncio
from dataclasses import replace

from runtime import AgentState, Effect, EffectKind, Result, Runner, Session
from hooks import Decision, DecisionAction, HookBus, HookCtx, HookMode

CALLS: list[dict] = []


async def call(effect: Effect, ctx) -> Result:
    """记录每次 Port 调用收到的 effect，返回带 model 的结果便于断言改写。"""
    CALLS.append(dict(effect.payload))
    if effect.kind is EffectKind.LLM:
        return Result.success({"content": f"model={effect.payload.get('model')}"})
    return Result.success(None)


async def dispatch(bus: HookBus, effect: Effect, ctx: HookCtx):
    """把总线裁决交给 R1。"""
    return await bus.emit("before_invoke", ctx)


def run_with(hook) -> tuple[str, str]:
    """跑一次带 hook 的 LLM effect，返回 (status, output)。"""
    async def go():
        CALLS.clear()
        bus = HookBus()
        bus.on(hook)
        bus.freeze()
        runner = Runner(
            dispatch=lambda e, c: dispatch(bus, e, c),
            call=call, ctx_factory=bus.ctx_factory(),
        )
        def program(state):
            resp = yield Effect(EffectKind.LLM, {"messages": [], "model": "gpt-x"})
            # fallback 回填的是裸值（字符串），正常 LLM 回填的是 dict
            if isinstance(resp, dict):
                content = resp.get("content", "?")
            elif isinstance(resp, Result):
                content = resp.data.get("content", "?") if isinstance(resp.data, dict) else "?"
            else:
                content = str(resp)
            yield Effect(EffectKind.END, {"output": content})
        result = await runner.run(Session(session_id="ext", program_factory=lambda: program(AgentState())))
        return result.status, str(result.output)
    return asyncio.run(go())


class ContinueHook:
    """放行 hook。"""
    name = "cont"; events = ("before_invoke",); mode = HookMode.GUARD
    priority = 10; requires_ports = ()
    async def __call__(self, ctx: HookCtx) -> Decision:
        return Decision.continue_()


class DenyHook(ContinueHook):
    """拒绝 hook。"""
    name = "deny"
    async def __call__(self, ctx: HookCtx) -> Decision:
        return Decision.deny("blocked")


class FallbackHook(ContinueHook):
    """兜底 hook。"""
    name = "fb"
    async def __call__(self, ctx: HookCtx) -> Decision:
        return Decision.fallback("FALLBACK")


class ModelSwapAround:
    """around 包裹：把 effect.model 改成 claude。"""
    name = "swap"; events = ("before_invoke",); mode = HookMode.AROUND
    priority = 10; requires_ports = ()
    async def __call__(self, ctx: HookCtx) -> Decision:
        ctx.effect = replace(ctx.effect, payload={**ctx.effect.payload, "model": "claude"})
        return Decision.continue_()


def test_continue_not_mistaken_for_abort():
    """回归：continue 不得被 Decision.abort 方法误判（历史 bug #1）。"""
    status, output = run_with(ContinueHook())
    assert status == "done" and "gpt-x" in output


def test_deny_aborts():
    """deny 生效：不调 Port，run 失败。"""
    status, _ = run_with(DenyHook())
    assert status == "error"
    assert not CALLS  # deny 不应调用 Port


def test_fallback_uses_value():
    """fallback 生效：用插件值作为结果，不调 Port。"""
    status, output = run_with(FallbackHook())
    assert status == "done" and "FALLBACK" in output
    assert not CALLS


def test_around_rewrite_effect_and_single_call():
    """around 改写生效且 Port 只调一次（历史 bug #2/#3）。"""
    status, output = run_with(ModelSwapAround())
    assert status == "done" and "claude" in output
    assert len(CALLS) == 1, f"Port 被调用 {len(CALLS)} 次（应恰好 1 次）"
    assert CALLS[0]["model"] == "claude"
