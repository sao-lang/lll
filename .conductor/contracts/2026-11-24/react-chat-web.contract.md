# change-contract · react-chat 真正可用聊天助手（2026-11-24，场景4 full）

## Why
用户要把 demo 变成真正可用的聊天助手：Web UI + 跨会话长期记忆 + 多轮对话 + 真实天气 + 搜索 + 链路追踪可视化。全走 MCP，不新增依赖。

## 用户原话
"main.py 做成真正可用，并且有长期记忆，记得跨会话，记得多轮对话，可以查询真实天气，可以搜索资料，接了链路追踪可视化的聊天助手"
用户澄清：交付形态=**Web UI 聊天+可视化**；工具后端=**全走 MCP**。

## Delta
新增：examples/react-chat 的 MCP server 脚本 ×2、FileMemoryStore、FileEventStore、Web UI（HTML/JS）、server.py 扩展 /api/chat；examples/trace-viewer 补 app.html + 重写 subject.py。
删除：main.py 的假天气与假 LLM 默认路径（离线仍保留显式降级）。
无兼容层：旧报告式 main.py 直接替换为 Web UI 入口（保留 CHAT_OFFLINE 环境变量）。

## 决策（采用）

### D1 交付形态：Web UI（标准库 http.server + SSE + 单文件 HTML/JS）
- 复用 `examples/trace-viewer/server.py` 的 HTTP+SSE 骨架（已可跑）；补缺失的 `app.html`。
- 新增 `POST /api/chat`（聊天消息）与会话隔离；保留 `/api/events` SSE 推实时相位 + 终态 trace。
- **不引入新依赖**（无 FastAPI/前端框架）——标准库足够，符合 demo 定位。

### D2 工具全走 MCP（用户明确选择）
- 写两个**轻量 MCP server 脚本**（Python 标准库，JSON-RPC over stdio，实现 `initialize`/`tools/list`/`tools/call`），履约框架 `MCPTransport` 协议：
  - `mcp_weather.py`：`get_weather(city)` → 调 open-meteo 免费 API（真实天气，无密钥）。
  - `mcp_search.py`：`search(query)` → 调公开搜索端点（DuckDuckGo HTML/Lite，无密钥；失败诚实返回 error，不造假）。
- 接法走框架原设计：`MCPManager(registry, StdioMCPTransport(channel))` → `connect()` → tools 注册进同一 `ToolRegistry`（本轮 A3 已修门面接线：`ToolsCapability(mcp=..., mcp_servers=...)`）。
- 离线 `CHAT_OFFLINE=1` 时 MCP server 返回明确 `offline` 标记，UI 显示降级（不伪造天气/搜索结果）。

### D3 长期记忆跨会话（文件持久化，注入式，不改框架内核）
- 框架现状：`MemoryStore` 五个内存 dict、`Manager.memory` 具体类、无文件实现；`Agent(store=)` 是 checkpoint 不是记忆。
- 方案：在 **examples 层**写 `FileMemoryStore`（继承 `MemoryStore`，覆写 `save/load` 用 JSON 落盘到 `examples/react-chat/.data/memory.json`）——**不改 src 内核**（工作记忆 R1 铁律：内核零领域语义；demo 层适配合理）。
- 跨会话：Web UI 会话 id 持久（本地存 session id；每会话一个 memory 文件）；重启进程后 `FileMemoryStore.load()` 恢复条目。
- 多轮对话：`Agent.history` 内存 + checkpoint EventStore 文件化（`Agent(store=FileEventStore)`）——`FileEventStore` 实现 R1 `EventStore`（save/load JsonSnapshot），落 `.data/checkpoints/<sid>.json`。这同时让"记得多轮对话"跨重启。

### D4 链路追踪可视化
- 数据源：`examples.react_chat.app.chat()` 返回 timeline/spans/audit/history（由 R1 事件与 trace 投影）；`subject.py` 保留为 CLI 侧兼容适配器（Web server 不经过它）。
- `app.html` 画：① 聊天区（消息列表+输入框）② 每次回答的链路时间线（span/component/duration/tokens/cost，SSE 实时增量）③ 审计链状态 ④ 工具调用明细。
- 保留 `agent.on()` 16 相位通知 → `trace-live` topic → SSE。

### D5 诚实边界（必须写进 UI 与文档）
- 密钥只走环境变量（DEEPSEEK_API_KEY），不落文件。
- 无密钥/离线时不伪造天气与搜索；显示"离线降级"。
- 搜索走公开 HTML 端点，可能受限/变更——失败显示原始错误，不包装成成功。
- 记忆文件是本地 JSON（非加密），demo 级持久化，不用于生产敏感数据。

## Invariants
- 内核 src/ R1~R10 零改动；能力包主线（注入能力包）不变。
- 密钥只走环境变量 DEEPSEEK_API_KEY，不落任何文件。
- 离线/失败不伪造数据，UI 明确显示降级与原始错误。
- MCP tools 经 connect 进同一 ToolRegistry（A3 门禁保护）。
- 工具层不 import IO SDK（HTTP 只在 examples MCP server 脚本内）。

## Affected
- `examples/react-chat/README.md`（新增 Web UI 运行方式与 MCP 配置）
- `.conductor/overviews/.../8ef577c1-...md`（overview 已声明）
- 契约本文件 + 覆盖矩阵 + 台账

## Open
- 框架层文件持久化 MemoryStore：本轮只在 examples 层做，src 内核不动（避免扩大变更面）；若后续要产品化再独立需求。
- MCP SDK 未安装：用标准库手写最小 JSON-RPC server，不装依赖。

## Acceptance
- AC-1 GET / 返回 200 且 HTML 含聊天输入框与时间线容器
- AC-2 POST /api/chat 返回非空 answer；两轮对话第二轮 system 含第一轮内容
- AC-3 FileMemoryStore/FileEventStore 落盘；重启后同 session 召回与历史恢复
- AC-4 在线 MCP 天气返回真实数据；离线返回 offline 标记不造假
- AC-5 MCP search 返回真实结果；失败显示原始错误
- AC-6 GET /api/trace 含 steps/actions/audit；SSE 推 live 事件
- AC-7 CHAT_OFFLINE=1 全链可跑；pytest/mypy/purity/模块 gate 全绿

## 验收（FR → 门禁）
见 wiring-fix-coverage 同目录 `react-chat-coverage.md`（必建，全勾销才进 TDD）。
