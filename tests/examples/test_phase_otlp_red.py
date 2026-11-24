"""FR-10 RED：PhaseOTLPBridge —— 16 相位通知 → OTLP span，Jaeger 现完整流程。

实证：真 chat 一轮触发 16 相位，但之前 Jaeger 只有 3 个自动埋点 span
（llm/tool:tool/llm），80% 流程不可见。本桥接把 16 相位转成 OTLP span：
- 注册 agent.on() 全部 16 相位；hook 只入队（同步、O(1)、不阻塞主流程）；
- 后台线程 drain → telemetry.export_span（复用同一 OTLP 通道）；
- 相位 span 与 llm/tool span 同 trace_id（Jaeger 串成一条链）；
- payload（phase/port/exceeded/step/status/error）作为 OTLP attributes。

先 RED（类不存在），实现后 GREEN。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


class _Receiver(BaseHTTPRequestHandler):
    spans: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        for rs in body.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    type(self).spans.append({
                        "name": s["name"], "traceId": s["traceId"],
                        "spanId": s["spanId"],
                        "attrs": {a["key"]: a["value"] for a in s["attributes"]},
                        "start_ns": int(s.get("startTimeUnixNano", 0)),
                        "end_ns": int(s.get("endTimeUnixNano", 0)),
                        "parent": s.get("parentSpanId", ""),
                    })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass


def test_red_phase_bridge_exports_all_phases_to_jaeger(
    tmp_path: Path, monkeypatch: object,
) -> None:
    import examples.react_chat.app as app

    _Receiver.spans = []
    srv = HTTPServer(("127.0.0.1", 0), _Receiver)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT",  # type: ignore[attr-defined]
                           f"http://127.0.0.1:{port}")
        monkeypatch.setattr(app, "_DATA", tmp_path)  # type: ignore[attr-defined]

        from examples.react_chat.app import build_agent_async, chat, close_agent

        async def _run() -> None:
            agent, _ = await build_agent_async("phase-e2e", offline=True)
            # build_agent_async 已自动 attach 相位桥（不重复建，避免 double bridge）
            bridge = getattr(agent, "_phase_bridge", None)
            assert bridge is not None, "自动接线未挂相位桥"
            try:
                result = await chat(agent, "查天气", session_id="phase-e2e")
                assert result["answer"], "相位桥不应阻断聊天"
            finally:
                await close_agent(agent)  # 内部会 drain 桥
        asyncio.run(_run())

        names = [s["name"] for s in _Receiver.spans]
        # 完整流程相位都在（不只是 llm/tool）
        for expected in ("phase:session_start", "phase:before_model",
                         "phase:after_model", "phase:before_tool",
                         "phase:after_tool", "phase:session_end"):
            assert expected in names, f"缺相位 {expected}：{names}"
        # 相位 span 与 llm/tool span 同 trace_id（Jaeger 串成一条链）
        trace_ids = {s["traceId"] for s in _Receiver.spans}
        assert len(trace_ids) == 1, f"trace 断裂：{trace_ids}"
        # payload 作为 attributes 上报（不是只上报名字）
        budget = next(s for s in _Receiver.spans if s["name"] == "phase:budget_check")
        assert "exceeded" in budget["attrs"] or "phase" in budget["attrs"]
        # B1 修复：相位 span 有父子树（session_start 为根，其余挂其下）
        phases = [s for s in _Receiver.spans if s["name"].startswith("phase:")]
        roots = [s for s in phases if not s["parent"]]
        assert len(roots) == 1, f"相位链路树应只有一个根：{len(roots)}"
        assert roots[0]["name"] == "phase:session_start"
        assert all(s["parent"] == roots[0]["spanId"] for s in phases
                   if s["spanId"] != roots[0]["spanId"]), "相位未挂在 session 根下"
        # B1 修复：时间戳是真实到达时刻（相位间 start_ns 单调递增，不全是导出时刻）
        starts = [s["start_ns"] for s in phases]
        assert starts == sorted(starts), f"相位时间非单调：{starts[:5]}"
        assert all(s["end_ns"] > s["start_ns"] for s in phases)
    finally:
        srv.shutdown()


def test_red_phase_hook_does_not_block_main_flow() -> None:
    """hook 只入队（同步 O(1)），慢 export 在后台线程，不阻塞主流程。"""
    from compose import LifecyclePhase, Notice
    from examples.react_chat.phase_otlp import PhaseOTLPBridge
    import time as _time

    class _SlowTelemetry:
        def __init__(self) -> None:
            self.exported = 0
        def export_span(self, span: object, *, extra_attrs: object = None) -> None:
            _time.sleep(0.15)  # 模拟慢 OTLP HTTP
            self.exported += 1

    slow = _SlowTelemetry()
    bridge = PhaseOTLPBridge(agent=None, telemetry=slow)  # type: ignore[arg-type]
    bridge.attach()
    t0 = _time.monotonic()
    for _ in range(5):
        bridge._on_notice(Notice(phase=LifecyclePhase.BEFORE_MODEL, port="llm"))
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.1, f"hook 阻塞主流程 {elapsed:.2f}s（应只入队）"
    bridge.close()


def test_red_build_agent_auto_attaches_phase_bridge(tmp_path: Path, monkeypatch: object) -> None:
    """接线：有 OTLP endpoint → build_agent_async 自动挂相位桥；无 → 不挂。"""
    import examples.react_chat.app as app

    async def _check(endpoint: str, expect_bridge: bool) -> None:
        if endpoint:
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)  # type: ignore[attr-defined]
        else:
            monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)  # type: ignore[attr-defined]
        monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
        monkeypatch.setattr(app, "_DATA", tmp_path)  # type: ignore[attr-defined]
        agent, _ = await app.build_agent_async("auto-bridge", offline=True)
        try:
            bridge = getattr(agent, "_phase_bridge", None)
            assert (bridge is not None) == expect_bridge, (
                f"endpoint={endpoint!r} 但相位桥挂载={bridge is not None}")
        finally:
            await app.close_agent(agent)

    asyncio.run(_check("http://127.0.0.1:14399", True))
    asyncio.run(_check("", False))
