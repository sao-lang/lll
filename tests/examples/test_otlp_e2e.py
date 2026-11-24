"""OTLP 真链路 E2E：真实 chat() → 框架自动埋点 → OTLP/HTTP 上报 → 假 Jaeger 接收端。

不用 MockTransport（那是单测自证）——这里起一个真 HTTP 服务器接收 POST，
走完整 Agent 装配 + asay + Trace.span 自动埋点，断言收到 llm/tool:tool/llm
三个 span、traceId 一致、duration 真实。证明"自动埋点→导出"链真通，
而非直接调 _register/_replace 合成 span。

诚实边界：这是假接收端（验证上报协议与链路），不是真 Jaeger UI；
真 Jaeger 需 Docker daemon 运行 + docker-compose 起 all-in-one（README）。
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
                        "duration_ns": int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"]),
                        "parentSpanId": s.get("parentSpanId", ""),
                    })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass


def test_e2e_real_chat_exports_spans_to_otlp_receiver(
    tmp_path: Path, monkeypatch: object
) -> None:
    import examples.react_chat.app as app

    _Receiver.spans = []
    srv = HTTPServer(("127.0.0.1", 0), _Receiver)  # 端口 0 = 系统分配
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT",  # type: ignore[attr-defined]
                           f"http://127.0.0.1:{port}")
        monkeypatch.setattr(app, "_DATA", tmp_path)  # type: ignore[attr-defined]

        from examples.react_chat.app import build_agent_async, chat, close_agent

        async def _run() -> None:
            agent, meta = await build_agent_async("otlp-e2e-test", offline=True)
            assert "OTLP" in meta["trace_backend"]
            try:
                result = await chat(agent, "查天气", session_id="otlp-e2e-test")
                assert result["answer"], "聊天主流程被 OTLP 上报阻断"
            finally:
                await close_agent(agent)
        asyncio.run(_run())

        names = [s["name"] for s in _Receiver.spans]
        assert "llm" in names, f"未收到 llm span：{names}"
        assert any(n.startswith("tool:") for n in names), f"未收到工具 span：{names}"
        # 同一 trace：所有 span 的 traceId 一致（链路串起来）
        trace_ids = {s["traceId"] for s in _Receiver.spans}
        assert len(trace_ids) == 1, f"traceId 不一致：{trace_ids}"
        # 真实耗时（非伪造 0）
        assert any(s["duration_ns"] > 0 for s in _Receiver.spans)
    finally:
        srv.shutdown()
