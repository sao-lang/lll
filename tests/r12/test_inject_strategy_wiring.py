"""回归：inject_strategy 注入的 port 策略真进执行链（体检 B3 复核：正确路径本就生效）。

用一个自定义 Arch 显式 Invoke(port="memory")，注入 spy memory 策略，
断言其 read() 被真实调用。注意：cognition 策略（Planner/Reflector 等）走
Arch 构造器注入（见 test_arch_strategy_wiring_red.py），不走本路径。
"""
from __future__ import annotations

from typing import Iterator

from api import Agent
from compose import PortCtx
from runtime import Invoke, Terminate


class _SpyMemory:
    name = "spy_memory"
    port = "memory"

    def __init__(self) -> None:
        self.calls = 0

    async def read(self, namespace: str, key: str, ctx: PortCtx) -> object:
        self.calls += 1
        return "injected-memory-value"

    async def write(self, namespace: str, key: str, value: object, ctx: PortCtx) -> None:
        self.calls += 100  # 实质语句（空实现会被履约校验拒绝）

    async def forget(self, namespace: str, key: str, ctx: PortCtx) -> None:
        self.calls += 200  # 实质语句


class _MemoryArch:
    name = "memory_arch"

    def compile(self):
        def program() -> Iterator[object]:
            out = yield Invoke(port="memory", payload={
                "op": "read", "namespace": "p", "key": "k"})
            yield Terminate(output=str((out or {}).get("value", out) if isinstance(out, dict) else out))
            return None
        return program()

    def static_check(self):
        class R:
            ok = True
            errors = ()
            ports_required = ("memory",)
        return R()


def test_red_inject_strategy_reaches_execution() -> None:
    """注入的 memory 策略方法必须被真实调用（证明 inject_strategy 对自定义 Arch 生效）。"""
    spy = _SpyMemory()
    agent = Agent(_MemoryArch())
    agent.inject_strategy(spy)

    answer = agent.say("read memory")

    assert spy.calls >= 1, f"注入的 memory.read() 应被调用，实得 {spy.calls}"
