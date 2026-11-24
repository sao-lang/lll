"""FR-1/2/9 RED：共享装配核心——离线全链可跑、记忆落盘、多轮历史注入。"""
from __future__ import annotations

import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def test_red_build_agent_offline_has_mcp_tools(tmp_path: Path, monkeypatch: object) -> None:
    from examples.react_chat.app import build_agent_async as build_agent
    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]

    import asyncio
    from examples.react_chat.app import close_agent
    async def _run() -> None:
        agent, meta = await build_agent("s1", offline=True)
        assert meta["offline"] and "memory" in meta["memory_path"]
        # 真断言：MCP 工具经 connect 进执行链 registry（不是只判 agent 非空）
        from examples.react_chat.app import chat
        await chat(agent, "你好", session_id="s1")
        tools = [sp.name for sp in agent.runtime.tools.catalog(level=1)]
        assert any(n.startswith("mcp__") for n in tools), f"MCP 工具未进 registry：{tools}"
        await close_agent(agent)
    asyncio.run(_run())


def test_red_chat_two_turns_history_and_persistence(tmp_path: Path, monkeypatch: object) -> None:
    from examples.react_chat.app import build_agent_async as build_agent, chat
    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]

    import asyncio
    from examples.react_chat.app import close_agent
    async def _run() -> None:
        agent, meta = await build_agent("s2", offline=True)
        try:
            first = await chat(agent, "你好", session_id="s2")
            second = await chat(agent, "再说一次", session_id="s2")
            assert first["answer"] and second["answer"]
            assert len(second["history"]) >= 2, "多轮历史未累积"
            assert Path(meta["checkpoint_path"]).exists(), "checkpoint 未落盘"
            assert Path(meta["memory_path"]).exists(), "记忆未落盘"
        finally:
            await close_agent(agent)
    asyncio.run(_run())


def test_red_history_survives_restart(tmp_path: Path, monkeypatch: object) -> None:
    """FR-3：多轮对话跨重启——新 Agent 同 session 应恢复上一轮问答历史。"""
    from examples.react_chat.app import build_agent_async, chat, close_agent
    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]

    async def _run() -> None:
        a, _ = await build_agent_async("hist-s", offline=True)
        try:
            await chat(a, "第一轮问题", session_id="hist-s")
        finally:
            await close_agent(a)
        b, _ = await build_agent_async("hist-s", offline=True)
        try:
            after = await chat(b, "第二轮问题", session_id="hist-s")
            assert len(after["history"]) >= 4, (
                f"重启后历史未恢复（只有 {len(after['history'])} 条）")
        finally:
            await close_agent(b)
    asyncio.run(_run())


def test_red_offline_llm_calls_mcp_every_turn(tmp_path: Path, monkeypatch: object) -> None:
    """B2：长驻 Agent 的每一轮问天气都应真触发 MCP 工具（不能只首轮）。"""
    from examples.react_chat.app import build_agent_async, chat, close_agent
    monkeypatch.setenv("CHAT_OFFLINE", "1")  # type: ignore[attr-defined]
    monkeypatch.setattr("examples.react_chat.app._DATA", tmp_path)  # type: ignore[attr-defined]

    async def _run() -> None:
        a, _ = await build_agent_async("multi-turn-mcp", offline=True)
        try:
            r1 = await chat(a, "查深圳天气", session_id="multi-turn-mcp")
            r2 = await chat(a, "再查深圳天气", session_id="multi-turn-mcp")
            tool1 = [s["name"] for s in r1["spans"] if s["name"].startswith("tool:")]
            tool2 = [s["name"] for s in r2["spans"] if s["name"].startswith("tool:")]
            assert tool1, "首轮未触发 MCP 工具"
            assert tool2, "第二轮未触发 MCP 工具（B2）"
        finally:
            await close_agent(a)
    asyncio.run(_run())
