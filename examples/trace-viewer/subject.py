"""链路追踪可视化 · 被追踪主体（复用 react-chat 的共享装配核心，单源真相）。

早期版本用 importlib 抓 main.py 的旧符号（TOOLS/_SystemPromptLLM 等），
已随门面重构失效。现在直接 import examples.react_chat.app 的 build_agent_async/chat，
与 CLI 与 Web 服务共用同一条装配链——不再复制实现、不再抓旧符号。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from examples.react_chat.app import build_agent_async, chat, close_agent  # noqa: E402

LIVE_TOPIC = "trace-live"


class TraceRun:
    """一次被追踪的运行：execute() 用共享核心跑 chat，返回全链路 trace。"""

    def __init__(self, prompt: str, offline: bool, session_id: str = "trace-viewer") -> None:
        self.prompt = prompt
        self.offline = offline
        self.session_id = session_id

    def execute(self) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            agent, meta = await build_agent_async(self.session_id, offline=self.offline)
            try:
                return chat_result(await chat(agent, self.prompt,
                                              session_id=self.session_id), meta)
            finally:
                await close_agent(agent)
        return asyncio.run(_run())


def chat_result(result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """把 chat() 的结果投影成 UI 友好的 trace 结构（steps/actions/totals/audit）。"""
    steps: list[dict[str, Any]] = []
    for span in result.get("spans", []):
        steps.append({"name": span["name"], "component": span.get("component", ""),
                      "duration_ms": span.get("duration_ms", 0.0),
                      "tokens": span.get("tokens", 0), "cost": span.get("cost", 0.0)})
    return {
        "steps": steps,
        "actions": result.get("timeline", []),
        "totals": {"turns": result.get("turns", 0), "tokens": result.get("tokens", 0),
                   "cost_usd": result.get("cost_usd", 0.0)},
        "audit": {"count": result.get("audit_count", 0),
                  "verified": result.get("audit_ok", False)},
        "events": result.get("timeline", []),
        "answer": result.get("answer", ""),
        "status": result.get("status", ""),
        "mode": meta.get("llm_label", ""),
    }
