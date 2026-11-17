"""R2-T08：错误信封结构 + hook 依赖端口装配期校验。"""
import pytest

from runtime import Result, PortError, AgentState, Effect, EffectKind
from hooks import HookBus, HookBusError, HookMode, HookCtx, Decision
from tests.r2.conftest import BaseHook


def test_result_error_envelope():
    r = Result.failure("E1", "msg", retryable=True)
    assert r.error == PortError(code="E1", message="msg", retryable=True)


def test_hook_requires_port_check():
    class NeedsPort(BaseHook):
        name = "needs"; events = ("before_invoke",); requires_ports = ("mystore",)
    bus = HookBus()
    bus.on(NeedsPort())
    with pytest.raises(HookBusError):
        bus.freeze()
    # 注册依赖后可 freeze
    bus.registry.register("mystore", type("S", (), {"name": "mystore"})())
    bus.freeze()
