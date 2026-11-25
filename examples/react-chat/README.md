# react-chat · 真正可用的聊式助手（Web UI + 全走 MCP + 长期记忆 + 链路追踪）

## 一句话

本地 Web 聊天助手：真实天气与资料搜索**全走 MCP**，长期记忆与多轮对话**跨会话/跨重启持久化**，每条回答都有**链路追踪可视化**。

## 启动

```bash
# Web UI（推荐）：一键启动器，自动从环境变量/注册表读取密钥，不落盘
.venv/Scripts/python examples/react-chat/start.py
# 浏览器打开 http://127.0.0.1:8510（服务会自动尝试打开）

# 或直接启动服务（需在终端先设好 DEEPSEEK_API_KEY 环境变量）
.venv/Scripts/python examples/trace-viewer/server.py

# 离线自检（不联网、不花钱；工具返回明确 offline 标记，绝不编造数据）
CHAT_OFFLINE=1 .venv/Scripts/python examples/react-chat/start.py

# CLI（同一装配核心，轻量入口）
CHAT_OFFLINE=1 CHAT_SESSION=my-session .venv/Scripts/python examples/react-chat/main.py "查深圳天气"
```

## 密钥配置（只走环境变量，绝不写进项目文件）

```cmd
:: Windows：设置用户级环境变量（一次即可，永久生效）
setx DEEPSEEK_API_KEY "sk-你的key"
```
`setx` 写入后，已打开的终端可能仍读到旧环境块——用 `start.py` 启动即可：
它运行时从注册表 User 区读取 key 注入进程（仅内存，不落文件），绕过该问题。
顶栏 badge 显示 `DeepSeek 真实 LLM` 即表示密钥已生效。

## 能力地图（每条都真实可验证）

| 能力 | 落点 | 验证 |
|---|---|---|
| 全走 MCP | `mcp_servers/weather_server.py`（open-meteo 免费 API）、`search_server.py`（DuckDuckGo HTML）→ 框架 `StdioMCPTransport` → `MCPManager.connect()` 注册进**同一 ToolRegistry** | `tests/examples/test_mcp_servers_red.py` + `test_mcp_tools_real_api_logic.py` |
| 跨会话长期记忆 | `FileMemoryStore`（examples 层 JSON 落盘，按 session_id 隔离）；新 Agent 同 session 自动恢复 | `test_persistence_red.py` + 重启 E2E |
| 多轮对话 | `Agent.history` + examples 层 history JSON（重启后注入）；checkpoint `FileEventStore` 另存 R1 快照 | `test_app_core_red.py::test_red_history_survives_restart` |
| 链路追踪可视化 | `app.html` 聊天区 + span/事件/审计时间线；`GET /api/events` SSE 推结果；`GET /api/trace` 全链路 JSON；span 带真实 tokens/cost | `test_web_e2e_red.py` + `test_trace_usage_red.py` |
| 流式输出 | 框架一等 `Agent.say_stream(text) -> AsyncIterator[str]`（走 `ModelGateway.stream_events()` 原生事件，逐 TEXT_DELTA yield）；页面 `GET /api/chat_stream` SSE 逐字显示，首字 ~0.9s（真 DeepSeek 实测 8 块） | `test_say_stream_red.py` + SSE 端到端实测 |
| 真实 LLM | 有 `DEEPSEEK_API_KEY` 走 DeepSeek OpenAI-compatible；无密钥/离线走明确标注的离线 LLM | `app.html` 顶部 badge 显示真实模式 |

## Web UI 用法

- 顶栏 badge 显示真实 LLM 模式（`DeepSeek 真实 LLM` / `离线模式` / `缺 DEEPSEEK_API_KEY`），由后端 `GET /api/state` 注入，不硬编码。
- 输入「用 get_weather 查一下深圳的天气」→ LLM 调 MCP 天气工具 → 右侧时间线出现 `tool:tool` span 与审计记录。
- 输入「search 一下 AgentKit」→ MCP 搜索工具返回真实标题/链接。
- 关闭浏览器再打开（同 session id）→ 记忆与历史自动恢复。

## 真实链路追踪（Jaeger / OTLP）—— 在链路平台上直接看完整走向

**无需任何胶水层**：框架 `Trace` 原生实现 `LifecycleBus` 插件协议（`name` +
`register(bus)`），门面装配期 `bus.subscribe(trace)` 自动接入。`session_start` 开
root span，16 相位 + llm/tool 全部挂其下，形成一棵同 `trace_id` 的树。设了
OTLP endpoint 后整棵树经 `OTLPTelemetry` 上报，Jaeger UI 直接呈现
「会话 → 相位 → LLM → 工具 → 输出」的完整走向。旧的 examples 层 `PhaseOTLPBridge`
胶水层已删除——不再需要。

```bash
# ① 起 Jaeger all-in-one（Podman 命名管道 API 或 Docker）
docker compose -f examples/trace-viewer/docker-compose.jaeger.yml up -d
#    （镜像 quay.io/jaegertracing/all-in-one:1.62.0；tag 必须是 1.62.0）
# ② 设 OTLP endpoint（与 DeepSeek key 一样走环境变量，start.py 也能从注册表读）
setx OTEL_EXPORTER_OTLP_ENDPOINT "http://127.0.0.1:4318"
# ③ 启动聊天（一键启动器读密钥/endpoint 注入进程，不落盘）
.venv/Scripts/python examples/react-chat/start.py
# ④ 打开 Jaeger UI：http://127.0.0.1:16686
#    Service 选 agentkit-react-chat → Find Traces → 点进 trace → 看链路树
```

- **链路树结构（实测 2 轮 = 43 span / 1 trace / 1 root）**：
  `session`（root）→ `turn:1`、`turn:2`（每轮一个分支）→ 各轮下挂
  `phase:session_start/user_prompt/budget_check/before_model/after_model/state_fold/
  before_tool/after_tool/tool_error/session_end` + `llm` + `tool:tool`。
  多轮对话是**一棵树**（不是多棵），Jaeger 上按 turn 展开就是整个会话走向。
  Trace 状态用 ContextVar 隔离，并发 chat 不串扰。
- span 真值来自框架 `TraceSpan`（name/component/duration_ms/tokens/cost），traceId 稳定映射为 OTLP hex。llm span 的 tokens/cost 由门面把 `Result.usage` 写回（prompt+completion、cost_usd），页面与 Jaeger 都显示真实账单（不再全 0）；`Trace.span` 退出时读回块内 usage 再记 duration，不覆盖真实值。
- **流式思考过程**：页面输入后走 `GET /api/chat_stream?message=...`，SSE `delta` 事件逐字追加到助手气泡；框架侧是 `Agent.say_stream()` 一等 API，直接消费 `gateway.stream_events()` 的 TEXT_DELTA，无 ContextVar 开关、无 topic 转手、无 examples 胶水。无流式后端时回退 complete 一次性 yield（诚实，不伪装逐字）。流式是单轮 LLM 对话；工具编排仍走 say/asay。
- 未设 `OTEL_EXPORTER_OTLP_ENDPOINT` → 用 NoopTelemetry，零网络 IO，demo 仍可离线跑（链路树照样在 `agent.trace.spans()` 里可查）。
- metric/log 本轮只内部留痕（traces only，诚实边界）；Jaeger 内存存储重启清空。
- **已真实验证**：本地起真 Jaeger 容器跑一轮 chat，`GET /api/traces?service=agentkit-react-chat`
  查到 1 条 trace、21 span、唯一 root `session`、相位+llm/tool 全挂其下（`tests/examples/
  test_phase_otlp_red.py` + `tests/r11/test_trace_lifecycle_tree_red.py` 守门）。
- 已知边界：OTLP 上报是同步 HTTP（`_replace` 内，最多阻塞 2s/span）；`export_failures()`
  计数未接到 UI；`TelemetryPort.span()` 手动入口产生 duration=0 的 span，仅供协议兼容
  （框架 `Trace.span` 才是主路径）。

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
