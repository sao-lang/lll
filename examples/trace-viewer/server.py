"""react-chat Web UI · 本地聊天服务（标准库 http.server + SSE + 持久事件循环）。

启动：
    .venv/Scripts/python examples/trace-viewer/server.py
    # 浏览器打开 http://127.0.0.1:8510

设计（契约 2026-11-24/react-chat-web）：
- 每个会话一个 Agent + 一个持久事件循环（MCP stdio 子进程绑定 loop，不可跨 loop）；
- POST /api/chat 把消息投递到会话 loop，返回本轮结果 + 链路追踪数据；
- GET /api/events SSE 推该会话的相位 tick + 结果；
- GET / 返回聊天 + 时间线 UI（app.html）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from examples.react_chat.app import build_agent_async, chat, close_agent  # noqa: E402

HOST = "127.0.0.1"
PORT = 8510
HTML_PATH = Path(__file__).resolve().parent / "app.html"


class Session:
    """一个聊天会话：独立 Agent + 持久 loop（MCP 子进程与 session_id 同源）。"""

    def __init__(self, session_id: str, offline: bool) -> None:
        self.id = session_id
        self.offline = offline
        self.loop = asyncio.new_event_loop()
        self.agent: Any = None
        self.meta: dict[str, Any] = {}
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.history: list[dict[str, str]] = []
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        """会话 loop 线程：构建 Agent 后永久运行（回收前保持）。"""
        asyncio.set_event_loop(self.loop)
        try:
            self.agent, self.meta = self.loop.run_until_complete(
                build_agent_async(self.id, offline=self.offline))
        except Exception as exc:  # noqa: BLE001 - 构建失败要让 API 层可见
            self.meta = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            self.ready.set()
        self.loop.run_forever()

    def submit(self, prompt: str) -> dict[str, Any]:
        """在持久 loop 里跑一轮 chat，阻塞至完成（HTTP 工作线程调用）。"""
        self.ready.wait()
        if not self.agent:
            return {"answer": "", "error": self.meta.get("error", "agent 构建失败")}
        future = asyncio.run_coroutine_threadsafe(
            chat(self.agent, prompt, session_id=self.id), self.loop)
        result = future.result(timeout=120)
        with self.lock:
            self.history = result.get("history", [])
            self.events.append({"type": "chat", "answer": result.get("answer", ""),
                                "spans": result.get("spans", []),
                                "status": result.get("status", "")})
        return result

    def close(self) -> None:
        """会话结束：关 MCP 子进程并停 loop。"""
        if self.agent:
            try:
                asyncio.run_coroutine_threadsafe(close_agent(self.agent), self.loop).result(5)
            except Exception:  # noqa: BLE001
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


class State:
    """服务级会话表（session_id → Session）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self.last: dict[str, Any] = {}
        self.version = 0

    def get_or_create(self, session_id: str, offline: bool) -> Session:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = Session(session_id, offline)
                self.sessions[session_id] = session
            return session

    def publish(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.last = result
            self.version += 1


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, event: str, payload: Any) -> bool:
        data = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            if not HTML_PATH.exists():
                self._json({"error": "app.html 缺失"}, status=500)
                return
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            # 后端真实模式注入（B1）：UI 不再硬编码 offline，按服务端配置走
            import os
            server_offline = os.getenv("CHAT_OFFLINE") == "1"
            has_key = bool(os.getenv("DEEPSEEK_API_KEY"))
            self._json({"sessions": list(STATE.sessions), "version": STATE.version,
                        "offline": server_offline,
                        "llm_mode": ("offline" if server_offline else
                                     ("deepseek" if has_key else "needs_key")),
                        "key_present": has_key})
        elif self.path == "/api/trace":
            self._json({"status": "ok", "trace": STATE.last})
        elif self.path.startswith("/api/events"):
            self._sse_stream()
        else:
            self._json({"error": "not found"}, status=404)

    def _sse_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_version = -1
        ping = 0.0
        import time
        while True:
            with STATE.lock:
                version = STATE.version
                result = dict(STATE.last)
            if version != last_version:
                last_version = version
                if result and not self._sse("chat", result):
                    return
            elif time.time() - ping > 4.0:
                ping = time.time()
                if not self._sse("ping", {"ts": ping}):
                    return
            time.sleep(0.2)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            self._json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, status=400)
            return
        prompt = str(body.get("message") or "").strip()
        if not prompt:
            self._json({"error": "message 不能为空"}, status=400)
            return
        session_id = str(body.get("session_id") or uuid.uuid4().hex[:8])
        offline = bool(body.get("offline", __import__("os").getenv("CHAT_OFFLINE") == "1"))
        try:
            session = STATE.get_or_create(session_id, offline)
            result = session.submit(prompt)
            STATE.publish(result)
            self._json(result)
        except Exception as exc:  # noqa: BLE001 - 错误上送 UI，不静默
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"react-chat 聊天助手已启动：{url}  （Ctrl+C 停止）")
    print("离线：CHAT_OFFLINE=1；真实 LLM：设 DEEPSEEK_API_KEY。工具全走 MCP。")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        for s in list(STATE.sessions.values()):
            s.close()


if __name__ == "__main__":
    main()
