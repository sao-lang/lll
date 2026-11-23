# 接线问题修复台账（2026-11-24，conductor 场景8 full）

> 主线约束：能力包是一等装配单位 + 组合根；用户注入能力包；能力包持策略+工具；
> 用户可自封装能力包。四条原设计（MCP tools→同一 ToolRegistry / 三级披露 /
> 记忆来源对话注入上下文 / 意图四级级联可替换）是接线约束。
> 已立门禁：tests/wiring/test_wiring_boundaries.py（10 条）；契约 6a12744。

## A 类：注入了但不生效（影子构造 / 死对象）——优先级最高

| id | 问题 | 证据 | 正确修法（不是全部接上） | 验收门禁 |
|---|---|---|---|---|
| [DONE] A1 | `SecurityCapability(audit=...)` 注入的 audit 在门面执行链零读取 | `src/api/` 只有 `self.audit=audit` 赋值；runtime.py:186 新建 `VerifyingAuditPort(AuditLogger(sink=...))` | 让 `assemble_runtime` 消费注入的 audit：有注入则用它包 Verifying，不再新建 AuditLogger；无注入才走默认 | G-W6 行为：注入 spy audit → 真工具调用后 spy 收到记录 |
| [DONE] A2 | `SecurityCapability(guardrail=...)` 输出脱敏用新建 `RuleGuardrail()` | wiring.py:233 `redact_output()` 新建默认；输入链 agent.py 已消费注入 guardrail | 输出脱敏复用注入实例的 guardrail（同一能力包），无注入才默认 | G-W6b：注入 spy guardrail → 输出脱敏经它 |
| [DONE] A3 | `ToolsCapability` 的 MCP transport 不进执行链 | install 注册 "mcp" port 到独立 Registry；_install_caps 跳过 tool；MCPManager.connect 无生产调用 | 按原设计：MCP tools 必须注册进**同一个 wired_tools**；门面提供 `tool_setup` 把 transport→MCPManager(reg, transport)→await connect() | G-W1 已有（库层）；新增门面级 G-W1b：注入 MCP transport → Agent 执行链可见 mcp__ tools |
| [DONE] A4 | 三级披露 `DisclosureIndex` 注入后无生产触发 | 全 src 无 `disclose()` 调用；设计文档说 before_model 触发 | 在组合层 before_model 相位触发披露（不新增门面 disclosure 入口）；门面不暴露内部细节 | G-W2 行为：before_model 后 L1/L2/L3 结构正确 |
| [OPEN-A5] A5 | `TelemetryExporter` 协议零消费点 | compose/ports.py 定义 + 导出外无任何 `.export()` 调用 | **不扩大范围**：本轮只确认边界并登记（诚实边界：接 OTel 需注入自定义 Trace/telemetry，非本轮修）。不新建无消费者的能力包 | 文档标注 + 扫描器嫌疑清单 |

## B 类：缺能力包入口（通用能力，协议现成）

| id | 问题 | 证据 | 正确修法 | 验收门禁 |
|---|---|---|---|---|
| [DONE] B1 | RAG 五段可注入但门面零入口 | RAGPipeline 构造参数齐全；src 无实例化；profile 只登记名字 | 建 `RAGCapability` 持有 pipeline + 可注入检索工具；门面 use() 后 register_port("rag") 真消费；不改 RAGPipeline 内部 | G-B1：注入 spy retriever → 架构 Invoke(port=rag) 真调 |
| [DONE] B2 | EvalPort 门面无入口，`evaluate()` 被反思能力占用 | cli.py 硬编码 RuleEval；agent.evaluate 走 ReflectionCapability | 建 `EvalCapability` 持 EvalPort；门面新增 `score_case()` 语义入口（不覆盖 evaluate 既有语义） | G-B2：注入 spy EvalPort → score_case 真调 |
| [DONE] B3 | Observability/Trace telemetry 门面不可注入 | Agent.trace 硬编码 Trace() 无 telemetry | 建 `ObservabilityCapability` 持有 telemetry/exporter；门面 trace 属性读能力包；不改 Trace 私有方法依赖 | G-B3：注入 spy telemetry → agent.trace.span 真进 spy |

## C 类：结构耦合（用户主线）

| id | 问题 | 状态 | 处理 |
|---|---|---|---|
| C1 | CognitionCapability 曾持有桥梁 | bb463d5 已修（能力包纯策略容器、桥梁解包 .strategy） | 门禁 G-W4/R1 保护，无需改 |
| C2 | 自定义能力包 provider op 硬编码 call | bb463d5 已修（provider.op 声明） | 门禁 G-W7 保护，无需改 |
| C3 | 意图四级级联 build 路径注入部件 | 963c46a 已修 | 门禁保护，无需改 |
| C4 | 记忆链来源对话/注入 system | 本来正确 | 门禁 G-W3 保护，无需改 |

## D 类：门禁/台账/文档

| id | 问题 | 处理 |
|---|---|---|
| D1 | 覆盖矩阵必建（full 档） | 本台账 + coverage matrix（需求原话→决策→FR→验收） |
| D2 | 每条修复必须有失败测试先行 | 每个 A/B 项先写 RED 门禁，再修 |
| D3 | 文档同步（公共契约/架构决策） | 契约 6a12744 更新 + README 诚实边界 |

## 修复顺序（依赖优先，避免返工）
1. A1/A2（安全审计链）——同源审计是其他能力的基础
2. A3/A4（工具/披露链）——按原设计接线
3. B1/B2/B3（能力包补齐）——复用能力包主线模板
4. A5 诚实边界登记（不修代码）
5. 全量门禁 + review + 文档同步 + commit

## 独立 review 结果（full 档必过）
- 2 Blocker 已修：
  1. 注入 audit 绕过验链 → runtime.py 对所有注入 audit 包 VerifyingAuditPort（G: test_audit_verification_gate.py）
  2. 披露死注入（产出丢弃+registry 不同源）→ bind_registry(rt.tools) + 披露包 L1/L2 进 LLM tools payload（G: test_disclosure_sources_gate.py）
- Should 登记（不修，带理由）：
  - S1 MCP 构造签名声明 MCPManager 但实际吃裸 transport → 文档澄清：注入 Transport（工具）。
  - S2 Trace 依赖 telemetry._register/_replace 私有方法 → OTel 接入须履约此内部契约，登记为已知边界。
  - S3 trace 懒建顺序：先读 trace 再 use(observability) 不生效 → 用户文档注明先 use 后读。
  - S4 默认路径不启用披露 → 设计决策：披露是 ToolsCapability 的能力，注入才启用（最小完整变更，不改 ReAct 既有 tools 语义）。
  - S5 部分测试手工调 rt.tools/handler → 装配是真装配，触发手工是为隔离 LLM 脚手架噪声；另有全链 say 测试覆盖。
- 终态门禁：pytest 全绿 / mypy strict 122 / purity rc=0 / wiring 19 条绿 / 独立 review Blocker=0。
