"""M2-T02/T03 七种 Verdict、重试计数与退避、首个 reject 生效。"""
from __future__ import annotations

import pytest

from agentkit.chains import Chains, ChainCtx, MountPoint, Verdict
from agentkit.chains.builtins import (
    AbortFilter, DegradeFilter, FallbackFilter, RedactResultFilter,
    RejectFilter, RetryFilter,
)
from agentkit.runtime import AgentState, Effect, EffectKind, Result
from tests.fakes import obj, run


def ctx_factory(invoke, payload=None) -> ChainCtx:
    ctx = ChainCtx(
        effect=Effect(EffectKind.LLM, payload or {"messages": [], "secret": "s3cr3t"}),
        state_view=AgentState(), session_id="verdict",
    )
    ctx.invoke = invoke
    return ctx


async def ok(effect, ctx):
    return Result.success(obj(content="ok"))


async def flaky(effect, ctx):
    if ctx.attempt < 2:
        return Result.failure("TIMEOUT", "timeout", retryable=True)
    return Result.success(f"ok@{ctx.attempt}")


async def always_fail(effect, ctx):
    return Result.failure("UPSTREAM", "down", retryable=True)


def build(filter_obj, *, at=MountPoint.AFTER_LLM) -> Chains:
    return Chains().filter(filter_obj, at=at)


def test_t02_allow_and_reject():
    out = run(build(RejectFilter("guard", "secret"), at=MountPoint.BEFORE_LLM).dispatch(
        ctx_factory(ok).effect, ctx_factory(ok)
    ))
    assert out.data.error.code == "FILTER_REJECTED"
    assert "port" not in "".join(out.trace)  # 去程 reject 不调 port


def test_t02_retry_skip_degrade_abort_redact():
    retry = run(build(RetryFilter("retry", times=2)).dispatch(ctx_factory(flaky).effect, ctx_factory(flaky)))
    assert retry.data.data == "ok@2" and retry.verdict.action == "allow"

    skip = run(build(FallbackFilter("fallback", "cached")).dispatch(
        ctx_factory(always_fail).effect, ctx_factory(always_fail)))
    assert skip.data.data == "cached" and skip.verdict.action == "skip"

    async def degrade_invoke(effect, ctx):
        return (Result.success(f"via:{ctx.meta['degrade_port']}")
                if ctx.meta.get("degrade_port") else Result.failure("UP", "down"))

    degrade = run(build(DegradeFilter("dg", "x-backup")).dispatch(
        ctx_factory(degrade_invoke).effect, ctx_factory(degrade_invoke)))
    assert degrade.data.data == "via:x-backup"

    abort = run(build(AbortFilter("abort")).dispatch(
        ctx_factory(always_fail).effect, ctx_factory(always_fail)))
    assert abort.abort and abort.verdict.action == "abort"

    redact = run(build(RedactResultFilter("redact", "safe")).dispatch(ctx_factory(ok).effect, ctx_factory(ok)))
    assert redact.data.data == "safe" and redact.verdict.action == "redact"


def test_t03_retry_stops_at_limit_and_backoff_is_applied():
    calls = []

    async def counting(effect, ctx):
        calls.append(ctx.attempt)
        return Result.failure("TIMEOUT", "still down")

    out = run(build(RetryFilter("r", times=2, backoff=0.001)).dispatch(
        ctx_factory(counting).effect, ctx_factory(counting)))
    assert calls == [0, 1, 2] and not out.data.ok


def test_t03_first_reject_wins():
    from agentkit.chains.builtins import AllowFilter
    chains = (Chains()
              .filter(RejectFilter("first", "secret"), at=MountPoint.BEFORE_LLM)
              .filter(AllowFilter("second"), at=MountPoint.BEFORE_LLM))
    out = run(chains.dispatch(ctx_factory(ok).effect, ctx_factory(ok)))
    assert out.data.error.code == "FILTER_REJECTED"
    assert "filter:first" in out.trace and "filter:second" not in out.trace


def test_t03_verdict_factories():
    assert Verdict.allow().action == "allow"
    assert Verdict.reject("no").reason == "no"
    assert Verdict("retry", retry=3, backoff=0.5).retry == 3


def test_t02_request_phase_degrade_and_skip():
    async def invoke(effect, ctx):
        return Result.success(effect.payload)

    c1 = Chains().filter(VerdictFixed("degrade", "x-alt"), at=MountPoint.BEFORE_LLM)
    ctx = ctx_factory(invoke)
    out = run(c1.dispatch(ctx.effect, ctx))
    assert ctx.meta["degrade_port"] == "x-alt" and out.data.ok

    c2 = Chains().filter(VerdictFixed("skip", "cached"), at=MountPoint.BEFORE_LLM)
    out2 = run(c2.dispatch(ctx_factory(invoke).effect, ctx_factory(invoke)))
    assert out2.data.data == "cached"


class VerdictFixed:
    """固定判决 filter（测试去程 degrade / skip / redact）。"""

    def __init__(self, action, data=None):
        self.name = f"fixed-{action}"
        self.action, self.data = action, data

    def check(self, ctx, phase):
        return Verdict(self.action, data=self.data) if phase == "request" else Verdict.allow()
