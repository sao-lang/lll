"""react-chat 共享装配核心（Web UI 与 CLI 复用，单源真相）。

设计（契约 2026-11-24/react-chat-web）：
- 能力包主线：Agent.use(ModelCapability / ToolsCapability / MemoryCapability /
  SecurityCapability / ResilienceCapability / ObservabilityCapability)；
- 工具全走 MCP：StdioMCPTransport → MCPManager.connect() → 同一 ToolRegistry（A3 门禁）；
- 长期记忆与多轮历史：examples 层 FileMemoryStore + FileEventStore（跨会话持久化）；
- 无密钥/离线不伪造：离线 LLM 与 MCP offline 标记。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from api import Agent, verify_audit_chain  # noqa: E402
from api.wiring import STREAM_TOPIC  # noqa: E402
from archs import ReAct  # noqa: E402
from compose import DangerLevel, LifecyclePhase, LLMRequest, LLMResponse, PortCtx, ToolCall, Usage  # noqa: E402
from harness.context import ContextMemoryManager, MemoryCapability  # noqa: E402
from harness.model import CostGuard, ModelCapability  # noqa: E402
from harness.observability import ObservabilityCapability  # noqa: E402
from harness.observability.telemetry import NoopTelemetry  # noqa: E402
from harness.resilience import ResilienceCapability, Timeout  # noqa: E402
from harness.security import SecurityCapability  # noqa: E402
from harness.tools import ToolsCapability  # noqa: E402
from ports.adapters.mcp import StdioMCPTransport, SubprocessStdioChannel  # noqa: E402
from runtime import Budgets  # noqa: E402

from .persistence import FileEventStore, FileMemoryStore  # noqa: E402

MODEL = os.getenv("CHAT_MODEL", "deepseek-chat")
MAX_COST_USD = 0.10
SYSTEM_PROMPT = (
    "你是简洁的中文助手。查天气用 get_weather，查资料用 search。"
    "回答基于工具返回的真实数据；工具返回 offline/error 时如实说明，不得编造。"
)
_DATA = _ROOT / "examples" / "react-chat" / ".data"
_MCP_DIR = _ROOT / "examples" / "react-chat" / "mcp_servers"


class OfflineLLM:
    """离线确定性 LLM（只在 examples，不冒充框架实现）。

    按最后一条用户消息里的关键词决定发天气/搜索工具调用或收尾，
    让离线也能真跑 MCP 链路（MCP server 自身返回 offline 标记）。
    """

    name = "offline_llm"
    last_usage = None

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._step = 0
        self._last_was_tool = False

    async def complete(self, req: LLMRequest, ctx: PortCtx) -> LLMResponse:
        self.calls.append([dict(m) for m in req.messages])
        usage = Usage(prompt_tokens=20, completion_tokens=10, cost_usd=0.0)
        names = [t.name for t in (req.tools_catalog or [])]
        last = ""
        for m in reversed(req.messages):
            if m.get("role") == "user":
                last = str(m.get("content") or "")
                break
        self._step += 1
        # 每轮都按"最后一条 user 消息"判定工具（B2：不能只看 _step==1，
        # Web 会话长驻 Agent 多轮都要真走 MCP）。工具调用返回后再发总结。
        summary = ("已按工具返回的真实数据答复。" if self._last_was_tool
                   else "（离线助手）已收到你的消息。离线模式下工具返回降级标记，不编造数据。")
        weather_name = next((n for n in names if "get_weather" in n), None)
        search_name = next((n for n in names if "search" in n), None)
        if not self._last_was_tool and weather_name and (
                "天气" in last or "weather" in last.lower()):
            self._last_was_tool = True
            return LLMResponse(content="", tool_calls=[ToolCall(
                call_id=f"tc-w-{self._step}", name=weather_name,
                args={"city": "深圳"})], usage=usage, model=self.name)
        if not self._last_was_tool and search_name and (
                "搜" in last or "查" in last or "search" in last.lower()):
            self._last_was_tool = True
            return LLMResponse(content="", tool_calls=[ToolCall(
                call_id=f"tc-s-{self._step}", name=search_name,
                args={"query": last})], usage=usage, model=self.name)
        self._last_was_tool = False
        return LLMResponse(content=summary, tool_calls=[],
                           usage=usage, model=self.name)

    async def stream(self, req: LLMRequest, ctx: PortCtx) -> Any:  # pragma: no cover
        raise NotImplementedError


def _build_llm(offline: bool) -> tuple[Any, CostGuard | None, str]:
    if offline:
        return OfflineLLM(), None, "离线 LLM（CHAT_OFFLINE=1）"
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("需要 DEEPSEEK_API_KEY 环境变量（或 CHAT_OFFLINE=1）")
    from ports.adapters.llm import OpenAICompatLLM
    return (OpenAICompatLLM(base_url="https://api.deepseek.com/v1",
                            api_key=api_key, model=MODEL),
            CostGuard(max_cost_usd=MAX_COST_USD), f"DeepSeek({MODEL})")


def _mcp_capability(offline: bool) -> ToolsCapability:
    """工具全走 MCP：两个 stdio server（天气/搜索），按原设计 connect 进同一 registry。"""
    py = sys.executable
    # demo 层显式 LOW：MCP 工具调用写审计（框架默认 SAFE 不写，保持内核契约不变）
    weather = StdioMCPTransport(
        SubprocessStdioChannel([py, str(_MCP_DIR / "weather_server.py")]),
        danger=DangerLevel.LOW)
    search = StdioMCPTransport(
        SubprocessStdioChannel([py, str(_MCP_DIR / "search_server.py")]),
        danger=DangerLevel.LOW)
    # 复用同一 transport 工厂：ToolsCapability 只持一个 transport 入口，
    # 这里用复合 transport 把两个 server 的 tools 合并（见 _MultiMCPTransport）。
    return ToolsCapability(mcp=_MultiMCPTransport(weather, search),
        mcp_servers=("weather", "search"))


class _MultiMCPTransport:
    """复合 MCP transport：把多个 server 聚合成单一 MCPTransport 视图。

    这是 examples 层适配器（不进 src）：MCPManager 按 server 名调用，
    复合体按名字路由到对应子 transport，tools/call 的工具名带 mcp__ 前缀由
    MCPManager 去掉后传入。握手由各子 transport 自己完成。
    """

    name = "multi_mcp"

    def __init__(self, weather: StdioMCPTransport,
                 search: StdioMCPTransport) -> None:
        self._servers = {"weather": weather, "search": search}

    async def list_tools(self, server: str) -> list[Any]:
        return await self._servers[server].list_tools(server)

    async def call_tool(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        return await self._servers[server].call_tool(server, tool, args)

    async def ping(self, server: str) -> bool:
        return await self._servers[server].ping(server)

    async def aclose(self) -> None:
        for t in self._servers.values():
            await t.aclose()


async def build_agent_async(session_id: str, *, offline: bool) -> tuple[Agent, dict[str, Any]]:
    """构造一个可用 Agent（能力包装配 + MCP 工具 + 持久化记忆）。

    必须在事件循环内调用：MCP stdio 子进程绑定当前 loop。
    """
    await asyncio.sleep(0)  # 明确 async 契约（无实际等待）
    llm, cost_guard, llm_label = _build_llm(offline)

    # 长期记忆：examples 层文件持久化，按会话隔离（FR-2）
    mem_store = FileMemoryStore(_DATA / "memory" / f"{session_id}.json")
    mem_cap = MemoryCapability(manager=ContextMemoryManager(memory=mem_store))

    # checkpoint：多轮对话跨会话（FR-3）
    event_store = FileEventStore(_DATA / "checkpoints" / f"{session_id}.json")

    # 门面 history 是 api 层字段（不在 R1 AgentState）→ 由 examples 层单独
    # 持久化问答对，重启后注入 Agent._history（不改框架内核）。
    history_path = _DATA / "history" / f"{session_id}.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    arch = ReAct([], llm=MODEL, max_turns=6)
    agent = Agent(
        arch, session_id=session_id, system=SYSTEM_PROMPT, store=event_store,
        budgets=Budgets(max_turns=10, max_tokens=8000, max_cost_usd=MAX_COST_USD))
    agent.use(ModelCapability(llm=llm, cost_guard=cost_guard))
    agent.use(SecurityCapability())
    agent.use(mem_cap)
    agent.use(ResilienceCapability(timeout=Timeout(timeout=30.0)))
    agent.use(ObservabilityCapability(telemetry=NoopTelemetry()))
    agent.use(_mcp_capability(offline))

    # 恢复历史：读 examples 层持久化的问答对 → 注入门面 _history
    if history_path.exists():
        try:
            import json
            restored = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(restored, list):
                agent._history = [dict(h) for h in restored
                                  if isinstance(h, dict)
                                  and h.get("role") in ("user", "assistant")]
        except (OSError, ValueError):
            pass  # 损坏文件不崩溃：从空历史开始（诚实降级）

    meta = {"llm_label": llm_label, "offline": offline,
            "memory_path": str(mem_store.path),
            "checkpoint_path": str(event_store.path),
            "history_path": str(history_path)}
    agent._app_meta = meta  # examples 层私有挂载，不改框架 Agent API
    return agent, meta


async def close_agent(agent: Agent) -> None:
    """显式关闭 MCP stdio 子进程（Web 会话结束时调用，避免句柄泄漏）。"""
    from harness.tools import ToolsCapability
    cap = next((c for c in agent._caps.values()
                if isinstance(c, ToolsCapability)), None)
    if cap is not None:
        transport = getattr(cap, "mcp_transport", None)
        close = getattr(transport, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001 - 清理失败不抛给调用方
                pass


async def chat(agent: Agent, prompt: str, *, session_id: str) -> dict[str, Any]:
    """异步聊天入口：await asay + 全链路诊断（同事件循环，MCP subprocess 不跨 loop）。

    注意：不能用同步 say()——它内部每次新建 asyncio.run，会让 MCP stdio
    子进程跨事件循环导致 I/O 崩溃（Windows proactor 显式报错）。
    """
    answer = await agent.asay(prompt)
    run = agent.last_run
    rt = agent.runtime
    timeline = []
    if rt is not None:
        try:
            timeline = [{"seq": e.seq, "type": e.type, "ts": e.ts,
                         "data": e.data} async for e in
                        agent.stream.subscribe(STREAM_TOPIC)]
        except Exception:  # noqa: BLE001 - 可视化失败不影响聊天主流程
            timeline = []
    spans = [{"name": s.name, "component": s.component,
              "duration_ms": s.duration_ms, "tokens": s.tokens, "cost": s.cost}
             for s in agent.trace.spans()]
    # 门面 history 落盘（examples 层）：跨会话多轮对话恢复
    meta = getattr(agent, "_app_meta", None)
    if meta and meta.get("history_path"):
        try:
            import json
            Path(meta["history_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(meta["history_path"]).write_text(
                json.dumps([dict(h) for h in agent.history], ensure_ascii=False, indent=2),
                encoding="utf-8")
        except (OSError, TypeError):
            pass  # 持久化失败不阻断聊天（诚实降级）
    return {
        "answer": answer,
        "status": run.status if run else "unknown",
        "reason": run.reason if run else "",
        "turns": run.state.budgets.used_turns if run else 0,
        "tokens": run.state.budgets.used_tokens if run else 0,
        "cost_usd": run.state.budgets.used_cost_usd if run else 0.0,
        "timeline": timeline,
        "spans": spans,
        "audit_count": len(rt.audit.events()) if rt else 0,
        "audit_ok": verify_audit_chain(rt.audit) if rt else False,
        "history": [dict(h) for h in agent.history],
        "session_id": session_id,
    }
