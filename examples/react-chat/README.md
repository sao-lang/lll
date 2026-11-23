# ReAct 聊天助手 · 框架能力全景 Demo

用 **真实 DeepSeek API** 跑一次 ReAct 聊天助手，把 AgentKit 框架里能用上的能力全部接线并可视化出来，用来评估框架是否可用。

## 运行

```bash
# 密钥只走环境变量，绝不写进代码/文件
export DEEPSEEK_API_KEY=sk-xxx        # Windows PowerShell: $env:DEEPSEEK_API_KEY="sk-xxx"
.venv/Scripts/python examples/react-chat/main.py
```

可选：`CHAT_PROMPT="深圳天气怎么样？"` 指定问题；`CHAT_OFFLINE=1` 用本地假 LLM 跑（不联网、不花钱，用于验证接线）。

## 覆盖的框架能力（均来自真实源码，非 mock）

| 能力 | 框架模块 | Demo 中如何体现 |
|---|---|---|
| ReAct 架构 | `archs.ReAct` | `static_check()` + `compile()` → R1 Program，`max_turns` 防御上限 |
| 真实 LLM | `ports.adapters.OpenAICompatLLM` + `harness.model.ModelGateway` | DeepSeek OpenAI-compatible 端点；`CostGuard` 调用前硬闸 |
| R1 执行内核 | `runtime.Runner/Session` | Effect 解释器、`Result`、`Budgets` 硬预算 |
| 工具四级路由 | `harness.tools.ToolRegistry` | SAFE 工具直执行；danger 以注册 spec 为准 |
| 预算管理 | `runtime.Budgets` + `api.Governance` | 6 维预算 + 成本护栏 + 熔断/灰度/特性开关 |
| 成本追踪 | `harness.model.CostAttributor` | 每次调用 usage 记账，`Result.usage` 喂给 R1 fold |
| 长期记忆 | `harness.memory.MemoryStore` | 会话前 `recall`、会话后 `ingest`（分层/去重/重要性） |
| 上下文管理 | `harness.context.ContextMemoryManager` | 组装、压缩视图（`commit_and_compact`）、大结果卸载 |
| 链路追踪 | `harness.observability.Trace/NoopTelemetry` | 手动 span 嵌套，记录 tokens/cost/metric/log |
| 事件流+投影 | `api.StreamGateway` + `TimelineProjection` | AFTER_MODEL/AFTER_TOOL/SESSION_END 相位事件 + 时间线投影 |
| 审计 | `api.VerifyingAuditPort` + `AuditLogger` | 工具调用哈希链 + 验链 |
| 安全护栏 | `harness.security.RuleGuardrail` | 注入检测（"ignore previous instructions" → deny） |
| 输出脱敏 | `api.wiring.redact_output` | secret_refs 命中即 redact |
| checkpoint | `api.CheckpointEventStore` | 每步落盘 + `replay_snapshot` 回放（R1 revive） |
| 评测 | `harness.eval.RuleEval` | 用本轮真实输出跑一次回归评分 |

## 实测发现的三个"接线必做项"（demo 里都已处理）

这三点不是框架 bug，但从 demo 里能明确看到——**直接用框架做真实 ReAct+DeepSeek 时必须自己补**：

1. **RuntimeCtx → PortCtx 适配**：R1 Runner 传给 `call` 的是 `RuntimeCtx`（无 `run_id`），而 `ModelGateway.complete` 的记账需要 `PortCtx.run_id`。不转换会在 `_record_cost` 抛 `AttributeError`。
2. **ReAct 消息格式归一化**：ReAct 内部产出的 `assistant.tool_calls=[{name,args,call_id}]`、`tool.content=dict` 不是 OpenAI 兼容格式（后者要求 `tool_calls[].id/type/function` 和 `tool_call_id` + 字符串 content）。直传会收到 DeepSeek `422`。demo 的 `_normalize_history()` 做了转换。
3. **usage 手动搬进 `Result.usage`**：`ModelGateway` 把 usage 放在返回 data 里，R1 `fold` 只读 `Result.usage`；不搬运则 `Budgets.cost/tokens` 不增长。

## 诚实边界（demo 会如实打印，不美化）

- 记忆/上下文/事件流/trace 默认都是**进程内内存实现**，无跨进程持久化与外部导出器
- trace 需**手动埋点**，Runner 不自动产生 span
- `CostGuard` 的 `cost_estimator` 是保守估算；DeepSeek 不返回 cost，所以账本 `cost_usd=0`（token 是真实的）
- `LocalSandbox` 是线程隔离，不是 OS 强沙箱；本 demo 用 LOW 工具演示审计链，不触发审批/沙盒
- 真实 LLM 调用默认关闭（`DEEPSEEK_API_KEY` 存在才跑），避免误扣费

## 预期输出

一份结构化报告：ReAct 每轮 LLM/工具调用、预算剩余、记忆召回命中、上下文压缩视图、trace span 树、事件流时间线、审计链、评测分数。
