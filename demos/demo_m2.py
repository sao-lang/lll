"""M2 独立闭环 demo：七挂载点 / 四泳道 / 六判决 / hook 隔离 / 与 M1 集成。

运行：make demo-m2
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agentkit.chains import Chains, MountPoint, Verdict  # noqa: E402
from agentkit.chains.builtins import (  # noqa: E402
    AbortFilter, AddMetaPipe, AllowFilter, BoomHook, DegradeFilter, FallbackFilter,
    PassMiddleware, PrefixPipe, RedactPipe, RedactResultFilter, RejectFilter,
    RetryFilter, TracingHook,
)
from agentkit.runtime import (  # noqa: E402
    AgentState, Effect, EffectKind, Result, Runner, Session,
)
from tests.fakes import FakeCall, llm_response, obj  # noqa: E402


def make_ctx(effect: Effect):
    from agentkit.chains import ChainCtx
    return ChainCtx(effect=effect, state_view=AgentState(), session_id="demo-m2")


async def invoke_ok(effect, ctx):
    return Result.success(obj(content="hello", tool_calls=[]))


async def invoke_flaky(effect, ctx):
    if ctx.attempt < 2:
        return Result.failure("TIMEOUT", "upstream timeout", retryable=True)
    return Result.success(obj(content=f"ok@attempt{ctx.attempt}", tool_calls=[]))


async def main() -> None:
    effect = Effect(EffectKind.LLM, {
        "messages": [{"role": "user", "content": "hi"}],
        "secret": "s3cr3t",  # 供去程脱敏 pipe / reject filter 演示
    })

    # ① 正常链路：七挂载点四泳道全部留名 + hook 故障隔离
    tracing = TracingHook()
    chains = (Chains()
              .pipe(AddMetaPipe("add-trace", "trace", True), at=MountPoint.BEFORE_LLM)
              .pipe(RedactPipe("redact-secret", "secret"), at=MountPoint.BEFORE_LLM)
              .pipe(PrefixPipe("prefix-result"), at=MountPoint.AFTER_LLM)
              .use(PassMiddleware())
              .filter(AllowFilter(), at=MountPoint.BEFORE_LLM)
              .filter(AllowFilter("output-check"), at=MountPoint.ON_OUTPUT)
              .hook(tracing)
              .hook(BoomHook()))
    ctx = make_ctx(effect)
    ctx.invoke = invoke_ok
    out = await chains.dispatch(effect, ctx)
    names = {t.split(":", 1)[1] for t in out.trace if ":" in t}
    print(f"[M2] allow verdict={out.verdict.action} data={out.data.data}")
    print(f"[M2] components in trace = {sorted(names)}")
    assert "add-trace" in names and "redact-secret" in names and "boom-hook:error" in str(out.trace)
    assert "hook:boom-hook:error:RuntimeError" in out.trace
    assert len(tracing.events) == 2  # request + response

    # ② 六种判决逐条验证
    async def verdict_case(components, invoke, *, at=MountPoint.AFTER_LLM):
        c = Chains()
        for comp, mount in components:
            c.filter(comp, at=mount)
        cx = make_ctx(effect)
        cx.invoke = invoke
        return await c.dispatch(effect, cx)

    reject = await verdict_case(
        [(RejectFilter("guard", "secret"), MountPoint.BEFORE_LLM)],
        invoke_ok,
    )
    assert reject.data.error.code == "FILTER_REJECTED"
    print(f"[M2] reject → {reject.data.error.code} (port never called)")

    retry = await verdict_case([(RetryFilter("retry", times=2), MountPoint.AFTER_LLM)], invoke_flaky)
    assert retry.data.ok and retry.data.data.content == "ok@attempt2"
    print("[M2] retry → attempt 0,1 fail → attempt 2 success")

    fallback = await verdict_case(
        [(FallbackFilter("fallback", "cached-answer"), MountPoint.AFTER_LLM)],
        invoke_flaky,
    )
    assert fallback.data.data == "cached-answer"
    print("[M2] skip → fallback 数据替换结果")

    async def invoke_degrade(effect, ctx):
        if ctx.meta.get("degrade_port"):
            return Result.success(f"served-by:{ctx.meta['degrade_port']}")
        return Result.failure("UPSTREAM", "primary down", retryable=True)

    degrade = await verdict_case(
        [(DegradeFilter("degrade", "x-llm-backup"), MountPoint.AFTER_LLM)],
        invoke_degrade,
    )
    assert degrade.data.data == "served-by:x-llm-backup"
    print("[M2] degrade → 切换备用 port")

    abort = await verdict_case([(AbortFilter("abort"), MountPoint.AFTER_LLM)], invoke_flaky)
    assert abort.abort is True
    print("[M2] abort → 终止 run 信号")

    redact = await verdict_case(
        [(RedactResultFilter("redact", "safe"), MountPoint.AFTER_LLM)],
        invoke_ok,
    )
    assert redact.data.data == "safe"
    print("[M2] redact → 结果替换")

    # ③ freeze 装配期检查
    from agentkit.chains import ChainConfigError
    from agentkit.chains.builtins import NeedsPortMiddleware
    c = Chains().use(NeedsPortMiddleware())
    try:
        c.freeze(required_ports=[])
        raise AssertionError("freeze 应拦截缺 port")
    except ChainConfigError:
        print("[M2] freeze → 缺 port 装配期报错")

    # ④ 与 M1 集成：Runner 注入 chains.dispatch / chains.ctx_factory
    def one_shot(state: AgentState):
        resp = yield Effect(EffectKind.LLM, {"messages": []})
        yield Effect(EffectKind.END, {"output": resp.content})

    call = FakeCall([llm_response("integrated")])
    runner = Runner(
        dispatch=chains.dispatch, call=call, ctx_factory=chains.ctx_factory(),
    )
    result = await runner.run(Session("demo-m2-run", lambda: one_shot(AgentState())))
    print(f"[M2+M1] status={result.status} output={result.output!r}")
    # 回程 pipe 只变换字符串数据；此处 Result.data 是 LLMResponse DTO，故原样透传
    assert result.status == "done" and result.output == "integrated"

    print("[M2] PASS: 四泳道 / 七挂载点 / 六判决 / hook 隔离 / 留名 / freeze / M1 集成")


if __name__ == "__main__":
    asyncio.run(main())
