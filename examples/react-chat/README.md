# react-chat · 真正可用的聊式助手（Web UI + 全走 MCP + 长期记忆 + 链路追踪）

## 一句话

本地 Web 聊天助手：真实天气与资料搜索**全走 MCP**，长期记忆与多轮对话**跨会话/跨重启持久化**，每条回答都有**链路追踪可视化**。

## 启动

```bash
# Web UI（推荐）：聊天界面 + 实时链路时间线
.venv/Scripts/python examples/trace-viewer/server.py
# 浏览器打开 http://127.0.0.1:8510（服务会自动尝试打开）

# 真实模式（需密钥，密钥只走环境变量，不落任何文件）
DEEPSEEK_API_KEY=sk-xxx .venv/Scripts/python examples/trace-viewer/server.py

# 离线自检（不联网、不花钱；工具返回明确 offline 标记，绝不编造数据）
CHAT_OFFLINE=1 .venv/Scripts/python examples/trace-viewer/server.py

# CLI（同一装配核心，轻量入口）
CHAT_OFFLINE=1 CHAT_SESSION=my-session .venv/Scripts/python examples/react-chat/main.py "查深圳天气"
```

## 能力地图（每条都真实可验证）

| 能力 | 落点 | 验证 |
|---|---|---|
| 全走 MCP | `mcp_servers/weather_server.py`（open-meteo 免费 API）、`search_server.py`（DuckDuckGo HTML）→ 框架 `StdioMCPTransport` → `MCPManager.connect()` 注册进**同一 ToolRegistry** | `tests/examples/test_mcp_servers_red.py` + `test_mcp_tools_real_api_logic.py` |
| 跨会话长期记忆 | `FileMemoryStore`（examples 层 JSON 落盘，按 session_id 隔离）；新 Agent 同 session 自动恢复 | `test_persistence_red.py` + 重启 E2E |
| 多轮对话 | `Agent.history` + examples 层 history JSON（重启后注入）；checkpoint `FileEventStore` 另存 R1 快照 | `test_app_core_red.py::test_red_history_survives_restart` |
| 链路追踪可视化 | `app.html` 聊天区 + span/事件/审计时间线；`GET /api/events` SSE 推结果；`GET /api/trace` 全链路 JSON | `test_web_e2e_red.py` + 浏览器实测 |
| 真实 LLM | 有 `DEEPSEEK_API_KEY` 走 DeepSeek OpenAI-compatible；无密钥/离线走明确标注的离线 LLM | `app.html` 顶部 badge 显示真实模式 |

## Web UI 用法

- 顶栏 badge 显示真实 LLM 模式（`DeepSeek 真实 LLM` / `离线模式` / `缺 DEEPSEEK_API_KEY`），由后端 `GET /api/state` 注入，不硬编码。
- 输入「用 get_weather 查一下深圳的天气」→ LLM 调 MCP 天气工具 → 右侧时间线出现 `tool:tool` span 与审计记录。
- 输入「search 一下 AgentKit」→ MCP 搜索工具返回真实标题/链接。
- 关闭浏览器再打开（同 session id）→ 记忆与历史自动恢复。

## 数据落点（`examples/react-chat/.data/`，gitignore）

```
memory/<session>.json       # 长期记忆条目
history/<session>.json      # 多轮问答对（门面 history）
checkpoints/<session>.json  # R1 checkpoint JsonSnapshot
```

## 诚实边界（不美化）

- **沙箱网络限制**：本开发环境外网 SSL 握手超时/证书链缺失，真实天气与搜索**无法在此环境联网验证**；MCP server 的 API 请求/解析逻辑已用可控 HTTP 替身测试（`test_mcp_tools_real_api_logic.py`），请在有网环境用真实 key/网络跑 `weather_server.py` / `search_server.py` 验证端到端。
- **失败不伪造**：网络/解析失败返回 `status:error` + 原始错误；离线返回 `status:offline`；UI 如实显示，绝不编造天气/搜索结果。
- **demo 级持久化**：本地 JSON，非加密、非并发安全、非生产存储；不要放敏感数据。
- **密钥只走环境变量** `DEEPSEEK_API_KEY`，不写入 `.data/`、不进提示词模板、不进 UI。
- **SSE 语义**：当前推每轮完整结果与 ping（结果推送为真）；契约中的"逐相位 tick"尚未在 Web 层展开（框架 16 相位通知本身真实可用，见 `agent.on()`）。
- **MCP 工具 danger**：MCP transport 默认 SAFE（直执行不写审计）；需要审计可将 server 工具声明为 LOW 并配置审批。
- **不新增依赖**：MCP server 用标准库 JSON-RPC over stdio；Web 用标准库 `http.server`；`pyproject.toml` 零改动。

## 文件结构

```
examples/react-chat/
  main.py                    # CLI 入口（共享装配核心）
  mcp_servers/_server.py     # 标准库 MCP server 骨架
  mcp_servers/weather_server.py / search_server.py
examples/react_chat/
  app.py                     # 共享装配核心（Web/CLI 单源）
  persistence.py             # FileMemoryStore + FileEventStore
examples/trace-viewer/
  server.py                  # Web 服务 + SSE
  app.html                   # 聊天 + 链路时间线 UI
  subject.py                 # trace 投影（复用 app.py）
tests/examples/              # 13+ 条真实行为门禁
```
