"""Lifecycle 通知体系契约：16 相位 + Notice 只读投影 + 异常就地隔离。"""
from __future__ import annotations

import pytest

from aop import Invocation
from hooks import LifecycleBus, LifecyclePhase, Notice, PHASE_COUNT
from runtime import AgentState, Invoke, Session


class _SessionView:
    session_id = "s-lc"


def _inv() -> Invocation:
    """构造最小 Invocation（effect/ctx/session/meta）。"""
    return Invocation(
        effect=Invoke(port="llm", payload={}),
        ctx=object(),
        session=_SessionView(),
        meta={"k": "v"},
    )


def test_phase_count_is_16():
    """通知相位恰为 16（口径自证）。"""
    assert PHASE_COUNT == 16
    assert len(list(LifecyclePhase)) == 16


def test_phase_names_exact():
    """16 相位名与规格一致，不含已移除的 compact 系列。"""
    names = {p.value for p in LifecyclePhase}
    assert names == {
        "session_start", "user_prompt", "session_end", "state_fold", "budget_check",
        "before_model", "after_model", "before_tool", "after_tool", "tool_error",
        "child_spawn", "child_done", "cancelled", "wait_human", "resumed",
        "invoke_rejected",
    }
    assert "before_compact" not in names and "after_compact" not in names


def test_notice_is_readonly_projection_of_invocation():
    """Notice 由 Invocation 投影，payload/meta 只读，不消费返回值。"""
    inv = _inv()
    notice = Notice.from_invocation(LifecyclePhase.BEFORE_MODEL, inv, payload={"model": "m"})
    assert notice.phase is LifecyclePhase.BEFORE_MODEL
    assert notice.session_id == "s-lc"
    assert notice.port == "llm"
    assert notice.payload["model"] == "m"
    assert notice.meta["k"] == "v"
    # 只读：不可写入
    with pytest.raises(TypeError):
        notice.payload["x"] = 1  # type: ignore[index]


def test_bus_dispatch_and_exception_isolation():
    """订阅者按相位收通知；单个订阅者抛异常被隔离，不影响其余订阅。"""
    bus = LifecycleBus()
    seen: list[str] = []

    def good(notice: Notice) -> None:
        seen.append(notice.phase.value)

    def bad(notice: Notice) -> None:
        raise RuntimeError("subscriber boom")

    bus.on(LifecyclePhase.BEFORE_MODEL, bad, name="bad")
    bus.on(LifecyclePhase.BEFORE_MODEL, good, name="good")
    bus.emit(Notice(phase=LifecyclePhase.BEFORE_MODEL))
    assert seen == ["before_model"]  # bad 被隔离，good 仍收到


def test_subscription_by_name_and_plugin_contract():
    """支持具名订阅与 name+register(bus) 插件契约。"""
    bus = LifecycleBus()
    seen: list[str] = []

    class Obs:
        name = "obs"
        def register(self, b: LifecycleBus) -> None:
            b.on(LifecyclePhase.SESSION_END, lambda n: seen.append(n.phase.value), name=self.name)

    bus.subscribe(Obs())
    bus.emit(Notice(phase=LifecyclePhase.SESSION_END))
    assert seen == ["session_end"]

    with pytest.raises(ValueError):
        bus.subscribe(object())  # 缺 name/register
