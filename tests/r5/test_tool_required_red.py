"""RED：ToolRegistry 必须校验 schema.required，缺失必填参数应 INVALID_ARGS。

裂缝：registry.call 只做 extra=forbid（非法键拒绝），不检查 required —— schema
声明 required=['x'] 但 args={} 仍 ok=True 执行，工具收到残缺参数。
修后：required 中每个键都必须出现在 args，否则 INVALID_ARGS（fail-closed）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compose import PortCtx, ToolCall, ToolSpec
from harness.tools.registry import ToolRegistry


def test_red_missing_required_rejected() -> None:
    spec = ToolSpec(name="t", description="", danger="SAFE",
                    parameters_schema={"type": "object", "properties": {"x": {"type": "integer"}},
                                       "required": ["x"]})
    called: list = []
    reg = ToolRegistry()
    reg.register(spec, lambda **kw: called.append(kw) or "OK")
    ctx = PortCtx(session_id="s", run_id="r")

    ok = asyncio.run(reg.call(ToolCall(call_id="c", name="t", args={}), ctx))
    assert not ok.ok and ok.error == "INVALID_ARGS", \
        f"缺 required x 应 INVALID_ARGS，实际 ok={ok.ok} error={ok.error}"
    assert called == [], "参数校验失败不得执行工具"

    good = asyncio.run(reg.call(ToolCall(call_id="c2", name="t", args={"x": 1}), ctx))
    assert good.ok and good.value == "OK", \
        f"参数齐全应正常执行，实际 ok={good.ok} error={good.error}"


def test_red_no_required_does_not_reject() -> None:
    spec = ToolSpec(name="t", description="", danger="SAFE",
                    parameters_schema={"type": "object", "properties": {"x": {}}})
    reg = ToolRegistry()
    reg.register(spec, lambda **kw: "OK")
    ctx = PortCtx(session_id="s", run_id="r")
    out = asyncio.run(reg.call(ToolCall(call_id="c", name="t", args={}), ctx))
    assert out.ok, f"无 required 声明时不应拒绝，实际 {out.error}"
