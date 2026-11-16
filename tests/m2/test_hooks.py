"""M2-T04 hook 故障隔离：观察者抛异常不影响主流程且记入 trace。"""
from __future__ import annotations

from chains import Chains, ChainCtx, MountPoint
from chains.builtins import AllowFilter, BoomHook, TracingHook
from runtime import AgentState, Effect, EffectKind, Result
from tests.fakes import run


class SyncHook:
    name = "sync-hook"

    def on_event(self, event, ctx):
        return None


def ctx() -> ChainCtx:
    c = ChainCtx(Effect(EffectKind.LLM, {"messages": []}), AgentState(), "hooks")
    c.invoke = lambda e, x: Result.success("ok")
    return c


def test_t04_boom_hook_is_isolated():
    chains = Chains().hook(BoomHook())
    out = run(chains.dispatch(ctx().effect, ctx()))
    assert out.data.ok and out.data.data == "ok"
    assert "hook:boom-hook:error:RuntimeError" in out.trace


def test_t04_hooks_observe_request_and_response():
    tracing = TracingHook()
    chains = (Chains().hook(tracing).hook(SyncHook())
              .filter(AllowFilter(), at=MountPoint.BEFORE_LLM))
    out = run(chains.dispatch(ctx().effect, ctx()))
    phases = {e.phase for e in tracing.events}
    assert phases == {"request", "response"}
    assert "hook:sync-hook" in out.trace
    assert all(e.effect.kind is EffectKind.LLM for e in tracing.events)


def test_t04_error_event_carries_port_error():
    tracing = TracingHook()
    chains = Chains().hook(tracing)
    c = ctx()
    c.invoke = lambda e, x: Result.failure("TIMEOUT", "down", True)
    out = run(chains.dispatch(c.effect, c))
    assert not out.data.ok
    errors = [e.error for e in tracing.events if e.phase == "response"]
    assert errors[-1].code == "TIMEOUT" and errors[-1].retryable
