"""react-chat 聊天助手 · CLI 入口（Web UI 的轻量替代）。

运行：
    # Web UI（聊天）：一键启动器自动读密钥/endpoint，不落盘
    .venv/Scripts/python examples/react-chat/start.py
    # 浏览器打开 http://127.0.0.1:8510

    # CLI 离线自检（不联网、不花钱）：
    CHAT_OFFLINE=1 .venv/Scripts/python examples/react-chat/main.py "查深圳天气"

    # CLI 真实（需 DEEPSEEK_API_KEY；天气/搜索走 MCP）：
    DEEPSEEK_API_KEY=sk-xxx .venv/Scripts/python examples/react-chat/main.py "搜一下 AgentKit"

链路追踪（在 Jaeger 平台看完整 AI 走向，无胶水层）：
    docker compose -f examples/trace-viewer/docker-compose.jaeger.yml up -d
    setx OTEL_EXPORTER_OTLP_ENDPOINT "http://127.0.0.1:4318"
    .venv/Scripts/python examples/react-chat/start.py   # 或本 CLI
    # 浏览器打开 Jaeger UI http://127.0.0.1:16686 → Service 选 agentkit-react-chat
    # → Find Traces → 点进去看「session → 相位 → llm → tool → 输出」的链路树

设计：装配核心在 examples/react_chat/app.py（Web 与 CLI 单源复用）；
工具全走 MCP；记忆/历史文件持久化；链路树由框架 Trace 原生产出；无密钥/离线不伪造数据。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from examples.react_chat.app import build_agent_async, chat, close_agent  # noqa: E402


async def _main() -> None:
    prompt = " ".join(sys.argv[1:]) or os.getenv(
        "CHAT_PROMPT", "用 get_weather 查一下深圳的天气，然后用一句话告诉我。")
    offline = os.getenv("CHAT_OFFLINE") == "1"
    session_id = os.getenv("CHAT_SESSION", "cli-default")

    agent, meta = await build_agent_async(session_id, offline=offline)
    try:
        result = await chat(agent, prompt, session_id=session_id)
    finally:
        await close_agent(agent)

    print("=" * 68)
    print(f"react-chat · {meta['llm_label']} · MCP 工具（天气/搜索）")
    print("=" * 68)
    print(f"问：{prompt}")
    print(f"答：{result.get('answer')}")
    print("-" * 68)
    print(f"status={result.get('status')} turns={result.get('turns')} "
          f"tokens={result.get('tokens')} cost=${result.get('cost_usd', 0):.6f}")
    print(f"审计链={result.get('audit_count')} 条 验链={'通过' if result.get('audit_ok') else '未验'}")
    print(f"链路 span={len(result.get('spans', []))} 条, 事件={len(result.get('timeline', []))} 条")
    print(f"记忆文件={meta['memory_path']}  历史文件={meta['checkpoint_path']}")
    print(f"多轮历史={len(result.get('history', []))} 条问答")
    print("=" * 68)
    print("链路树（session → turn:N → 相位/llm/tool）已由框架 Trace 产出；"
          f"后端={meta.get('trace_backend', '-')}")
    print("在 Jaeger 平台看走向：设 OTEL_EXPORTER_OTLP_ENDPOINT 后打开 "
          "http://127.0.0.1:16686 → Service=agentkit-react-chat")
    print("Web 聊天：.venv/Scripts/python examples/react-chat/start.py")


if __name__ == "__main__":
    asyncio.run(_main())
