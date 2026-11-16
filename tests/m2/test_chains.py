"""M2-T01 七挂载点 × 四泳道组合矩阵。"""
from __future__ import annotations

from chains import Chains, ChainCtx, MountPoint
from chains.builtins import AddMetaPipe, AllowFilter, PassMiddleware, TracingHook
from runtime import AgentState, Effect, EffectKind, Result
from tests.fakes import obj, run


def all_mount_chains() -> Chains:
    chains = Chains()
    for mount in MountPoint:
        chains.pipe(AddMetaPipe(f"pipe-{mount.value}", f"k-{mount.value}", 1), at=mount)
        chains.filter(AllowFilter(f"filter-{mount.value}"), at=mount)
        chains.use(PassMiddleware(f"mw-{mount.value}"), at=mount)
    chains.hook(TracingHook("hook-all"))
    return chains


def make_ctx(effect=None) -> ChainCtx:
    ctx = ChainCtx(
        effect=effect or Effect(EffectKind.LLM, {"messages": []}),
        state_view=AgentState(), session_id="m2",
    )
    ctx.invoke = lambda e, c: Result.success(obj(content="ok", tool_calls=[]))
    return ctx


def test_t01_every_mount_point_has_all_four_lanes():
    chains = all_mount_chains()
    seen: set[str] = set()
    for kind in (EffectKind.LLM, EffectKind.TOOL, EffectKind.SKILL):
        out = run(chains.dispatch(Effect(kind, {"messages": []}), make_ctx()))
        seen.update(t.split(":", 1)[1] for t in out.trace if t.startswith(("pipe:", "filter:", "middleware:", "hook:")))
    for mount in (MountPoint.ON_INPUT, MountPoint.ON_STATE, MountPoint.ON_OUTPUT):
        out = run(chains.run_mount(mount, make_ctx(), {"payload": 1}))
        seen.update(t.split(":", 1)[1] for t in out.trace if t.startswith(("pipe:", "filter:", "middleware:", "hook:")))
        assert out.data.ok
    for mount in MountPoint:
        for lane, prefix in (("pipe", "pipe"), ("filter", "filter"), ("middleware", "mw")):
            assert f"{prefix}-{mount.value}" in seen, (lane, mount, sorted(seen))
    assert "hook-all" in seen


def test_t01_mount_group_mapping():
    from chains import MOUNT_GROUP, DEFAULT_GROUP
    assert MOUNT_GROUP[EffectKind.LLM] == (MountPoint.BEFORE_LLM, MountPoint.AFTER_LLM)
    assert MOUNT_GROUP[EffectKind.TOOL] == (MountPoint.BEFORE_TOOL, MountPoint.AFTER_TOOL)
    assert DEFAULT_GROUP == (MountPoint.ON_INPUT, MountPoint.ON_OUTPUT)


def test_t01_pipes_transform_request_and_response():
    from chains.builtins import PrefixPipe
    chains = (Chains()
              .pipe(AddMetaPipe("add", "x", 1), at=MountPoint.BEFORE_LLM)
              .pipe(PrefixPipe("prefix"), at=MountPoint.AFTER_LLM))
    ctx = make_ctx()
    ctx.invoke = lambda e, c: Result.success("data")
    out = run(chains.dispatch(Effect(EffectKind.LLM, {"m": []}), ctx))
    assert out.effect.payload["x"] == 1 and out.data.data == "«data"


def test_t01_ctx_factory_from_runner():
    from runtime import Session
    chains = all_mount_chains().freeze()
    factory = chains.ctx_factory()
    ctx = factory(Effect(EffectKind.TOOL, {}), Session("s", lambda: iter(())))
    assert ctx.mount is MountPoint.BEFORE_TOOL and ctx.session_id == "s"
    assert ctx.budget_left is not None
