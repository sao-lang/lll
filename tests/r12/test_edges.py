"""R12-T10 §6.1.3 边界行为 reason 正确（TDD RED → GREEN）。"""
from __future__ import annotations

import asyncio

from runtime import Result, Runner, Session, Terminate, Invoke
from archs import ReAct, ToolUse, Router
from archs.base import StaticCheckReport
from compose import ToolSpec, DangerLevel


def _run(arch, *, call) -> tuple[str, object, str]:
    res = asyncio.run(Runner(call=call).run(
        Session(session_id="r12-edge", program_factory=lambda: arch.compile())))
    return res.status, res.output, res.reason


def test_tool_all_failed_reason() -> None:
    """tools 为空 → tool_all_failed。"""
    arch = ToolUse([], parallel=False)
    _, _, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert reason == "tool_all_failed"


def test_turns_exhausted_reason() -> None:
    """LLM 永不收敛 → turns_exhausted。"""
    arch = ReAct([ToolSpec(name="t", danger=DangerLevel.SAFE)], max_turns=2)
    _, _, reason = _run(arch, call=lambda e, c: Result.success(
        {"content": "loop", "tool_calls": [{"name": "t"}]}))
    assert reason == "turns_exhausted"


def test_no_route_reason() -> None:
    """Router 无 default 且 label 不在 arms → no_route。"""
    arch = Router(lambda t: None, {}, default=None)
    _, _, reason = _run(arch, call=lambda e, c: Result.success(None))
    assert reason == "no_route"


def test_graph_invalid_reason() -> None:
    """Graph 无 exit → 装配期 E04（不进运行期），reason 通过 static_check 暴露。"""
    from archs import Graph
    g = Graph().node("a", lambda: (yield Terminate(output=1)))
    assert "R12-E04" in {e.code for e in g.static_check().errors}
