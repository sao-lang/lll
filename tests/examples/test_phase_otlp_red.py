"""链路树 OTLP 上报 RED：框架原生 16 相位 + llm/tool 同一棵树 → Jaeger 可见走向。

实证：真 chat 一轮触发 16 相位。改造前 Jaeger 只有 3 个平级自动 span
（llm/tool:tool/llm），80% 流程不可见，相位靠 examples 层 PhaseOTLPBridge 胶水层。
改造后：框架 Trace 原生订阅 LifecycleBus，session_start 开 root span，相位+
llm/tool 全挂其下；OTLP 上报后平台直接呈现"会话→相位→LLM→工具→输出"的树，
零胶水层。本测试用真 OTLP/HTTP 接收端验收到达的 span 树。
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
                        "component": next((a["value"].get("stringValue")
                                           for a in s["attributes"]
                                           if a["key"] == "component"), ""),
                        "parent": s.get("parentSpanId", ""),
                    })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass


def test_red_framework_exports_phase_tree_to_jaeger(
    tmp_path: Path, monkeypatch: object,
) -> None:
    """真 chat 一轮 → OTLP 接收端收到同一棵链路树（root + 相位 + llm/tool）。"""
    _Receiver.spans = []
    srv = HTTPServer(("127.0.0.1", 0), _Receiver)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT",  # type: ignore[attr-defined]
                           f"http://127.0.0.1:{port}")
        monkeypatch.setattr(app_module(), "_DATA", tmp_path)  # type: ignore[attr-defined]

        from examples.react_chat.app import build_agent_async, chat, close_agent

        async def _run() -> None:
            agent, _ = await build_agent_async("phase-e2e", offline=True)
            try:
                result = await chat(agent, "查天气", session_id="phase-e2e")
                assert result["answer"], "链路不应阻断聊天"
            finally:
                await close_agent(agent)
        asyncio.run(_run())

        spans = _Receiver.spans
        names = [s["name"] for s in spans]
        # 关键相位都上报（不只是 llm/tool）
        for expected in ("phase:session_start", "phase:before_model",
                         "phase:after_model", "phase:before_tool",
                         "phase:after_tool", "phase:session_end"):
            assert expected in names, f"缺相位 {expected}：{names}"
        # 全部同 trace_id（Jaeger 串成一条链）
        trace_ids = {s["traceId"] for s in spans}
        assert len(trace_ids) == 1, f"trace 断裂：{trace_ids}"
        # 唯一 root（session）→ turn:1 容器 → 相位 + llm/tool
        roots = [s for s in spans if not s["parent"]]
        assert len(roots) == 1, f"链路树应只有一个根：{[r['name'] for r in roots]}"
        root_id = roots[0]["spanId"]
        assert roots[0]["name"] == "session"
        turns = [s for s in spans if s["name"].startswith("turn:")]
        assert len(turns) == 1, f"一轮应有一个 turn：{[t['name'] for t in turns]}"
        assert turns[0]["parent"] == root_id, "turn 应挂 session root"
        turn_id = turns[0]["spanId"]
        children = [s for s in spans if s["parent"] == turn_id]
        assert any(s["name"].startswith("phase:") for s in children), "相位未挂 turn"
        assert any(s["component"] == "llm" for s in children), "llm 未挂 turn"
        assert any(s["component"] == "tool" for s in children), "tool 未挂 turn"
        # 时间戳真实单调（相位到达时刻，不全是导出时刻）
        phases = [s for s in spans if s["name"].startswith("phase:")]
        starts = [s["start_ns"] for s in phases]
        assert starts == sorted(starts), f"相位时间非单调：{starts[:5]}"
    finally:
        srv.shutdown()


def test_red_no_phase_bridge_glue_in_examples() -> None:
    """胶水层已删：examples 不得再定义 PhaseOTLPBridge / 手动注册相位 hook。"""
    from pathlib import Path as _P
    glue = _P(ROOT / "examples" / "react_chat" / "phase_otlp.py")
    assert not glue.exists(), "PhaseOTLPBridge 胶水层应已删除（框架原生取代）"
    app_src = (ROOT / "examples" / "react_chat" / "app.py").read_text(encoding="utf-8")
    assert "PhaseOTLPBridge" not in app_src, "app.py 不应再引用相位桥胶水层"
    assert "_phase_bridge" not in app_src, "app.py 不应再挂载相位桥"


def test_red_phase_spans_exported_eagerly() -> None:
    """相位 span 必须 _register + _replace 一步完成（否则只存不上报）。"""
    from harness.observability import Trace
    from harness.observability.telemetry import NoopTelemetry

    class CountingTelemetry(NoopTelemetry):
        replaces = 0
        def _replace(self, ended: object) -> None:  # type: ignore[override]
            type(self).replaces += 1
            super()._replace(ended)  # type: ignore[arg-type]

    CountingTelemetry.replaces = 0
    trace = Trace(trace_id="t-export", telemetry=CountingTelemetry())
    # 直接驱动相位：session_start 开 root + phase 子 span，均须触发 _replace 上报
    from compose import LifecyclePhase, Notice
    trace._on_phase(Notice(phase=LifecyclePhase.SESSION_START, session_id="s"))
    trace._on_phase(Notice(phase=LifecyclePhase.BEFORE_MODEL, port="llm"))
    assert CountingTelemetry.replaces >= 2, (
        f"root + 相位须 _replace 上报，实际 replaces={CountingTelemetry.replaces}")


def app_module() -> object:
    import examples.react_chat.app as m
    return m
