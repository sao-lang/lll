"""RED：MCP tools/list 的 annotations.destructiveHint 必须提升 danger 级别。

裂缝：tools_from 用构造时统一 _danger（默认 SAFE）给所有工具，忽略每个工具的
annotations.destructiveHint=True → 破坏性 MCP 工具按 SAFE 裸奔，绕过沙盒/审批。
修后：destructiveHint=True 的工具至少 HIGH；用户显式传入的 danger 不被降级（取更严）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import DangerLevel
from ports.adapters.mcp import _JsonRpcPeer


def _tools_result(*annotations: dict) -> dict:
    return {"tools": [
        {"name": f"t{i}", "description": "", "inputSchema": {"type": "object"},
         "annotations": a}
        for i, a in enumerate(annotations)
    ]}


def test_red_destructive_hint_raises_danger() -> None:
    peer = _JsonRpcPeer(danger=DangerLevel.SAFE)
    specs = peer.tools_from(_tools_result(
        {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}))
    assert specs[0].danger is DangerLevel.HIGH, \
        f"destructiveHint=True 应至少 HIGH，实际 {specs[0].danger}"


def test_red_readonly_hint_stays_safe() -> None:
    peer = _JsonRpcPeer(danger=DangerLevel.SAFE)
    specs = peer.tools_from(_tools_result({"readOnlyHint": True, "destructiveHint": False}))
    assert specs[0].danger is DangerLevel.SAFE, \
        f"只读工具保持 SAFE，实际 {specs[0].danger}"


def test_red_explicit_critical_not_downgraded() -> None:
    peer = _JsonRpcPeer(danger=DangerLevel.CRITICAL)
    specs = peer.tools_from(_tools_result({"readOnlyHint": True, "destructiveHint": False}))
    assert specs[0].danger is DangerLevel.CRITICAL, \
        f"用户显式 CRITICAL 不得被只读注解降级，实际 {specs[0].danger}"
