# 需求覆盖矩阵：R8/R9/R10 实现（大需求门禁）

> 需求列逐条来自用户原话与三份详设验证矩阵（用户原话 = "R8 R9 R10 全量实现全绿；先拷问设计、解决问题再 coding；与已完成代码直接接线；未完成模块留功能接线并备注；禁止死代码/打补丁/兼容/测试桩"）。
> 收口前做一次模式切换红队复审（假设规格有漏，逐维找洞），结果记在 overview。

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| 用户：先拷问 R8/R9/R10 设计，结合各模块文档与总设计 | 采用 brainstorm 拷问产出 4 分叉 + 用户全选推荐 | FR-0 | AC-0 overview 拷问发现 A/B/C 三节 + 4 决策 |
| 用户：有问题先解决再 coding | 采用 设计冲突按源码事实裁决（A 修文档 / C 以代码签名准） | FR-0 | AC-0 contract Delta/Invariants 固定裁决 |
| 用户：与已完成代码直接接线 | 采用 Router._recognize→intent_as_program；ToolsCapability 注入 R9 三 Port | FR-1/FR-5 | AC-3/AC-4 |
| 用户：需未完成模块支持的留功能接线+备注 | 采用 依赖方向恒向下，无上依赖；Judge/OTel 等文档-only 项留 Affected 备注 | FR-9 | AC-10 文档同步核对 |
| 用户：禁止死代码/打补丁/兼容/测试桩 | 采用 source_purity --strict + 无兼容层 + fake 仅在 tests/ | FR-10 | AC-8 |
| R8-T01 意图识别 label/confidence/gaps | 采用 cognition 默认 KeywordMatcher + IntentResult | FR-1 | AC-1 |
| R8-T08 四级级联逐级短路/兜底/四部件注入/strategy 整体替换 | 采用 CascadeIntentStrategy + intent_as_program 桥梁 | FR-1 | AC-1 |
| R8-T02 规划执行 steps 游标正确 | 采用 确定性 Planner/Executor 默认 | FR-2 | AC-2 |
| R8-T03 反思轮换 max_rounds 处结束 | 采用 确定性 ReflectStrategy 默认 | FR-2 | AC-2 |
| R8-T04 10 架构端到端 | 采用 复用 R12 现成 10 架构，不重建 | FR-3 | AC-3 |
| R8-T05 组合算子 seq/par/branch/budget 生效 | 采用 复用 R12 A 类算子（budget 非算子以规格为准） | FR-3 | AC-3 |
| R8-T06 三层嵌套 Router→Graph→ReAct | 采用 复用 R12 嵌套语义 + Router 裂缝修复 | FR-3 | AC-3 |
| R8-T07 装配期检查悬空边/环/缺 port | 采用 R12 static_check 现成，纳入 r8 门禁 | FR-3 | AC-7 |
| R9-T01 输入护栏注入/敏感串拦截 | 采用 Guardrail + injection_guard 复用单检测器 | FR-4 | AC-4 |
| R9-T02 工具/检索结果含指令被拦截 | 采用 SecurityCtx from_invocation + 单检测器 | FR-4 | AC-4 |
| R9-T03 HIGH+ 进沙盒 | 采用 LocalSandbox 默认实现 + ToolRegistry 注入 | FR-5 | AC-4 |
| R9-T04 挂起→恢复→续跑 | 采用 ApprovalFlow + ConsoleHumanPort + WaitHuman/Resumed 通知 | FR-5 | AC-4 |
| R9-T05 审计覆盖 100%+哈希链 | 采用 AuditLogger append-only + hash 链 | FR-5 | AC-4 |
| R9-T06 secret 不进任何持久化 | 采用 secret_refs 引用注入 + 脱敏 | FR-4 | AC-4 |
| R10-T01 一个 trace_id 串联+组件留名 | 采用 Trace + NoopTelemetry span | FR-6 | AC-5 |
| R10-T02 订阅+从 seq 续读 | 采用 InMemoryEventStream + EventEnvelope | FR-6 | AC-5 |
| R10-T03 重放 State 与终态全等 | 采用 复用 R1 replay_events | FR-7 | AC-5 |
| R10-T04 checkpoint 跨进程恢复一致 | 采用 复用 R1 EventStore + CheckpointSnapshot fingerprint | FR-7 | AC-5 |
| R10-T05 三层评测不调真实 LLM | 采用 RuleEval + 确定性 Judge | FR-8 | AC-5 |
| 门禁体系可扩展 r8/r9/r10 | 采用 gate_check MODULES + ast_scan TARGETS + Makefile 目标 | FR-11 | AC-7 |
| 全量 pytest/mypy 无回归 | 采用 TDD RED→GREEN + 全量验证 | FR-10 | AC-6 |
| 独立 review Blocker=0 | 采用 全新上下文子代理复审 | FR-10 | AC-9 |
| 文档同步（总设/模块详设/门禁体系/README/ports 归属表/B.1.2） | 采用 Affected 清单逐条勾销 | FR-9 | AC-10 |
| R12 现成 10 架构行为不变（接线风险） | 采用 回归 test_archs_semantics 绿为证 | FR-3 | AC-3 |
| R5 装配行为不变（三 Port 注入风险） | 采用 回归 tests/r5 + test_e2e_wiring 绿为证 | FR-5 | AC-6 |
| OTel/OTLP 规范缺失（B.1.3） | 范围外 本次只做 NoopTelemetry 内存实现，OTLP 导出属后续独立项 | — | — |
| LLM-as-judge 真模型判别 | 否决 用户选确定性默认，不调真实 LLM | — | — |
| R10 自建 EventStore | 否决 复用 R1 EventStore | — | — |

## 模式切换红队复审（假设规格错/漏）
- 维度1 接线副作用：Router 改 `_recognize` 最怕破坏 R12 9 个架构语义测试 → 已列 AC-3 回归门禁。
- 维度2 Port 签名漂移：照文档写 `record(AuditEvent)` 会破坏 R5/registry → Invariants 固定以源码签名准，AC-4 覆盖。
- 维度3 源码纯净：默认实现若不落地会退化成桩 → AC-8 source_purity 硬门禁 + fake 仅 tests。
- 维度4 未完成模块依赖：Judge/OTel 是要后续支持的项 → 矩阵标"范围外"并在 Affected 备注，不留假实现。
- 维度5 文档一致性：总设 §R10.4 重复 EventStore、B.1.2 未移行 → AC-10 显式核对。
- 结论：五维均有对应 FR/验收或显式否决/范围外，无裸漏项。
