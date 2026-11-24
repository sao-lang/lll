# 需求覆盖矩阵 · react-chat Web UI（2026-11-24）

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R1 真正可用 | 采用：Web UI 聊天（POST /api/chat）+ 真实 LLM | FR-1 | E2E: server 启动 + 发消息 + 收到非空回答 |
| R2 长期记忆跨会话 | 采用：FileMemoryStore JSON 落盘、按会话隔离 | FR-2 | 重启进程后同 session 仍召回；测试断言文件存在且 load 还原 |
| R3 记得多轮对话 | 采用：Agent.history + FileEventStore checkpoint | FR-3 | 连发两轮，第二轮 system 含第一轮问答；重启后 history 恢复 |
| R4 查询真实天气 | 采用：MCP server (open-meteo)，StdioMCPTransport | FR-4 | 在线跑返回真实 temp/condition；离线返回明确 offline 标记 |
| R5 搜索资料 | 采用：MCP server (DDG HTML)，StdioMCPTransport | FR-5 | 在线跑返回真实标题/链接；失败返回原始错误 |
| R6 全走 MCP | 采用：ToolsCapability(mcp=transport) → connect 进同一 registry | FR-6 | 门面 catalog 含 mcp__* tools（复用 A3 门禁） |
| R7 链路追踪可视化 | 采用：app.html + SSE + TraceRun._build_trace | FR-7 | 浏览器可见聊天+时间线；/api/trace 返回 steps/actions |
| R8 Web UI 交付形态 | 采用：标准库 http.server + SSE + 单文件 HTML | FR-8 | GET / 返回 200 HTML；SSE 推 live/status |
| R9 无密钥不伪造 | 采用：离线降级标记，不造假数据 | FR-9 | CHAT_OFFLINE=1 全链可跑，UI 显示降级 |
| R10 不改框架内核 | 采用：持久化与 MCP server 均在 examples 层 | FR-10 | src/ 零改动（除已修接线）；purity rc=0 |
| R11 不新增依赖 | 采用：标准库 MCP JSON-RPC + http.server | FR-11 | pyproject.toml 零改动 |

FR 数 = 11（full 档覆盖矩阵必建）。
