"""FR-1/7/8 E2E RED：Web 服务启动 + 聊天 + trace + 会话隔离。"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples" / "trace-viewer"))


def _wait(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except Exception:
            time.sleep(0.2)
    raise AssertionError("server not up: " + url)


def test_red_web_chat_e2e(tmp_path: Path, monkeypatch: object) -> None:
    import server as web
    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]
    web.PORT = 8599
    srv = web.ThreadingHTTPServer((web.HOST, web.PORT), web.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://{web.HOST}:{web.PORT}"
    try:
        _wait(base + "/")
        html = urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8")
        assert "聊天助手" in html and "messages" in html
        body = json.dumps({"message": "你好", "session_id": "e2e-1", "offline": True}).encode()
        req = urllib.request.Request(base + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
        assert resp.get("answer"), f"无回答：{resp}"
        assert resp.get("session_id") == "e2e-1"
        trace = json.loads(urllib.request.urlopen(base + "/api/trace", timeout=3).read())
        assert trace["trace"]["answer"]
    finally:
        for s in list(web.STATE.sessions.values()):
            s.close()
        srv.shutdown()


def test_red_web_state_exposes_real_llm_mode(tmp_path: Path, monkeypatch: object) -> None:
    """B1：Web 状态接口必须暴露真实 LLM 模式（UI 不再硬编码 offline=true）。"""
    import server as web
    monkeypatch.setenv("CHAT_OFFLINE", "0")  # type: ignore[attr-defined]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")  # type: ignore[attr-defined]
    web.PORT = 8601
    srv = web.ThreadingHTTPServer((web.HOST, web.PORT), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://{web.HOST}:{web.PORT}"
    try:
        _wait(base + "/api/state")
        state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=3).read())
        assert state["offline"] is False, "服务端模式未下发"
        assert state["llm_mode"] == "deepseek" and state["key_present"]
        html = urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8")
        assert "let offline = null" in html, "UI 仍硬编码 offline"
    finally:
        srv.shutdown()
