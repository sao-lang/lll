"""M2-T05/T06 顺序确定性快照 + 组件留名（透明性红线）。"""
from __future__ import annotations

from agentkit.chains import Chains, ChainCtx, MountPoint, require_name
from agentkit.chains.builtins import (
    AddMetaPipe, AllowFilter, PassMiddleware, TracingHook,
)
from agentkit.runtime import AgentState, Effect, EffectKind, Result
from tests.fakes import run


def configured() -> Chains:
    return (Chains()
            .pipe(AddMetaPipe("p1", "a", 1), at=MountPoint.BEFORE_LLM)
            .pipe(AddMetaPipe("p2", "b", 2), at=MountPoint.BEFORE_LLM)
            .use(PassMiddleware("mw1"))
            .use(PassMiddleware("mw2"))
            .filter(AllowFilter("f1"), at=MountPoint.BEFORE_LLM)
            .filter(AllowFilter("f2"), at=MountPoint.AFTER_LLM)
            .hook(TracingHook("h1")))


def ctx() -> ChainCtx:
    c = ChainCtx(Effect(EffectKind.LLM, {"messages": []}), AgentState(), "trace")
    c.invoke = lambda e, x: Result.success("ok")
    return c


def test_t05_registration_order_is_execution_order():
    first = run(configured().dispatch(ctx().effect, ctx())).trace
    second = run(configured().dispatch(ctx().effect, ctx())).trace
    assert first == second  # 顺序确定性快照
    lanes = [t for t in first if t.startswith(("pipe:", "middleware:", "filter:", "hook:"))]
    assert lanes.index("pipe:p1") < lanes.index("pipe:p2")
    assert lanes.index("middleware:mw2") < lanes.index("middleware:mw1")  # 洋葱：后注册的先被包裹


def test_t06_every_component_leaves_its_name():
    out = run(configured().dispatch(ctx().effect, ctx()))
    for name in ("p1", "p2", "mw1", "mw2", "f1", "f2", "h1"):
        assert any(t.endswith(f":{name}") for t in out.trace), (name, out.trace)


def test_t06_require_name_rejects_anonymous():
    import pytest
    from agentkit.chains import ChainConfigError

    class Anonymous:
        def transform(self, data, ctx):
            return data

    with pytest.raises(ChainConfigError):
        Chains().pipe(Anonymous(), at=MountPoint.BEFORE_LLM)
    assert require_name(AllowFilter("named"), "filter") == "named"
