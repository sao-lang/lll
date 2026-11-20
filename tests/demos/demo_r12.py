"""R12 算子核心 demo：装配 A 类算子 + 守卫 + call 装饰器，验证组合主线。

独立冒烟（不依赖 pytest）：打印 PASS 表示算子核心装配与 dispatch 正常。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from runtime import Invoke, Result, Runner, Session, Terminate  # noqa: E402

from compose import Verdict  # noqa: E402
from compose import (  # noqa: E402
    branch, fallback, gate, loop, map_effect, map_state, par, race, retry, seq,
)
from compose.call import build_call as _build_call  # noqa: E402
from compose.dispatch import make_dispatch  # noqa: E402


class _AuditGuard:
    """②守卫 demo：记录 check/observe，验证跨调用状态留在实例。"""
    name = "audit"

    def __init__(self) -> None:
        self.seen = 0

    async def check(self, effect: object, ctx: object) -> Verdict | None:
        return None

    async def observe(self, effect: object, result: Result, ctx: object) -> None:
        self.seen += 1


def _leaf(name: str) -> object:
    def program():
        yield Invoke(port="llm", payload={"name": name})
        yield Terminate(output=name)
    return program


def main() -> None:
    """装配并跑通：seq + map_effect + build_call + dispatch + 守卫。"""
    ops = (seq, par, branch, race, loop, map_state, map_effect, fallback, gate, retry)
    assert all(callable(op) for op in ops), "10 算子必须齐全"

    seen: list[str] = []
    def handler(effect: Invoke, ctx: object) -> Result:
        seen.append(str(effect.payload.get("name")))
        return Result.success("ok")

    guard = _AuditGuard()
    call = _build_call(make_dispatch({"llm": handler}), guards=[guard])  # type: ignore[list-item]
    runner = Runner(call=call)
    program = seq(map_effect(lambda e: e.with_payload(demo=True))(_leaf("a")), _leaf("b"))
    result = asyncio.run(runner.run(Session(session_id="demo-r12", program_factory=program)))  # type: ignore[arg-type]
    assert result.status == "done" and result.output == "b"
    assert seen == ["a", "b"]
    assert guard.seen == 2
    # P6：10 内置架构 = A 类算子预设配方，static_check + compile 冒烟
    from archs import Graph, ReAct, ToolUse
    from compose import ToolSpec
    arch = ToolUse([ToolSpec(name="t1", description="d")], parallel=False)
    assert arch.static_check().ok
    assert asyncio.run(runner.run(Session(session_id="arch", program_factory=lambda: arch.compile()))).status == "done"  # type: ignore[arg-type]
    graph = Graph().node("a", _leaf("a")).exit("a")
    assert graph.static_check().ok
    print("R12 算子核心 demo PASS：10 算子齐全 + seq/map_effect + build_call/dispatch + 守卫跨调用状态 + 内置架构冒烟")


if __name__ == "__main__":
    main()
