"""Lifecycle 通知体系契约：16 相位 + Notice 只读投影 + 异常就地隔离。"""
from __future__ import annotations

import pytest

from compose import Invocation
from compose import LifecycleBus, LifecyclePhase, Notice, PHASE_COUNT
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


def test_error_hook_receives_subscriber_exception():
    """订阅者异常隔离时，可选 on_error 钩子收到 (订阅者名, 相位, 异常)；默认静默。"""
    errors: list[tuple[str, str, str]] = []
    bus = LifecycleBus(on_error=lambda name, phase, exc: errors.append((name, phase.value, str(exc))))

    def bad(notice: Notice) -> None:
        raise RuntimeError("boom")

    def good(notice: Notice) -> None:
        errors.append(("good", notice.phase.value, "ok"))

    bus.on(LifecyclePhase.AFTER_TOOL, bad, name="bad")
    bus.on(LifecyclePhase.AFTER_TOOL, good, name="good")
    bus.emit(Notice(phase=LifecyclePhase.AFTER_TOOL))

    assert errors[0][0] == "bad" and errors[0][1] == "after_tool" and "boom" in errors[0][2]
    assert errors[1][0] == "good"  # 异常隔离仍成立：坏订阅者不影响好订阅者


def test_default_bus_is_silent_on_error():
    """未注入 on_error 时，异常仍就地静默隔离（向后兼容）。"""
    bus = LifecycleBus()
    bus.on(LifecyclePhase.AFTER_TOOL, lambda n: (_ for _ in ()).throw(RuntimeError("x")), name="bad")
    bus.on(LifecyclePhase.AFTER_TOOL, lambda n: None, name="good")
    bus.emit(Notice(phase=LifecyclePhase.AFTER_TOOL))  # 不抛穿即通过


def test_error_hook_self_exception_does_not_escape():
    """on_error 钩子自身抛异常时被二次隔离，绝不从 emit 抛穿（铁律），后续订阅者照常收到。"""
    seen: list[str] = []

    def bad_hook(name, phase, exc):
        raise RuntimeError("hook itself exploded")

    bus = LifecycleBus(on_error=bad_hook)

    def bad_sub(notice: Notice) -> None:
        raise RuntimeError("sub exploded")

    bus.on(LifecyclePhase.AFTER_MODEL, bad_sub, name="bad")
    bus.on(LifecyclePhase.AFTER_MODEL, lambda n: seen.append("good"), name="good")
    bus.emit(Notice(phase=LifecyclePhase.AFTER_MODEL))  # 不抛穿即通过
    assert seen == ["good"]
