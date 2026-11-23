# 需求覆盖矩阵 · R5/R9/R10/R11 边界修复（大需求门禁）

> 需求列逐条抄用户原话 + 拷问发现的问题；模式切换红队复审（假设规格有漏，逐维找洞）已执行，见文末。

| 需求 | 决策 | FR | 验收 |
|---|---|---|---|
| R5 现状只在 LOW 工具写审计，HIGH/CRITICAL 不写；补齐写入点需改 R5 | 采用：统一 record 覆盖 LOW/HIGH/CRITICAL 三分支 | FR-1 | AC-3 / tests/r11/test_boundary_fix.py |
| 审计链是独立哈希链，不与 R1 事件日志同源 | 采用：审计事件以 JsonSnapshot 落 R1 EventStore 保留 session，replay 重建（用户选推荐"委托 R1 EventStore"） | FR-2 | AC-4 / replay + verify_hash_chain |
| EventEnvelope 的 seq/ts 是合成值（Notice 无时钟字段） | 采用：ts 注入真实 clock；Notice 加 seq 字段透传 R1 事件序号（用户选推荐） | FR-3 | AC-5 / 注入时钟断言精确值 |
| CRITICAL 路径审批通过后仍需注入 SandboxPort 才能执行，否则 fail-closed | 采用：从 SecurityCapability 取 sandbox 注入 ToolRegistry（用户选推荐） | FR-4 | AC-6 / 生产路径 CRITICAL 真执行 |
| 你先根据设计拷问代码，主要核查各模块逻辑的合理性，以及模块之间是否完全联通 | 采用：三路并行只读拷问，发现 B1/B2 + S1~S7 | FR-0 | overview 1150 调查记录（证据 文件:行号） |
| 找到问题后和上面的问题一起修掉 | 采用：修已知四项 + B1/B2 + 可明确修的 Should（用户选推荐范围） | FR-1~FR-8 | AC-1~AC-7 |
| B1 审计事件字段全量错配，生产审计恒空壳但链验过 | 采用：registry 写入字段与 AuditLogger 消费字段统一为一份具名 schema | FR-1 | AC-3 / 字段非空断言 |
| B2 两条装配链平行空转，profile 装的 sandbox/human/audit 进不了 ToolRegistry | 采用：assemble_runtime 消费 SecurityCapability 三 Port，删 _NoHuman | FR-4 | AC-6 / 装配链合一断言 |
| S1 stream_bus 双 bus，getter 返回的 bus 无消费者 | 采用：Runner.notify 与 getter 复用同一 bus | FR-5 | AC-6 / bus is 断言 |
| S3 LOW 审计缺席 fail-open（标 audit_missing 仍执行） | 采用：审计缺席 fail-closed | FR-6 | AC-3/AC-7 |
| S4 ApprovingHumanPort.ask 未传 danger，trigger_danger 配置死结 | 采用：R5 路由传 danger=spec.danger.value | FR-7 | AC-7 / trigger_danger 生效断言 |
| S5 ToolsCapability.install 单参违反 Capability 两参协议 | 采用：改签名为 install(container, reg) | FR-8 | AC-7 / 两参调用断言 |
| S7 CheckpointEventStore 双写两套 store | 采用：audit sink 与 checkpoint 共享单一 R1 EventStore | FR-2 | AC-4/AC-7 / 单 store 断言 |
| S2 after_model 无生产触发（model_gateway 无生产调用者） | 进 Open：真实 LLM 装配时接入，留功能接线契约+文档标注，不造假实现 | — | — |
| R1 一行不改（项目硬约束） | 采用：Event.ts 仍逻辑时钟，墙钟只在组合层/R11 注入 | FR-3 | AC-5 / git diff src/runtime/ 空 |
| 零死代码/兼容层/测试桩 | 采用：删 _NoHuman/audit_missing 降级语义，fake 仅 tests | FR-6 | AC-9 / source_purity rc=0 |
| 失败测试先行 + 证据只认退出码 | 采用：TDD RED→GREEN + verify.py + source_purity + gate | FR-9 | AC-1/AC-8/AC-9 |
| 改公共契约必须同步文档 | 采用：R5/R9/R10/R11 + 总设同步，0 死链 | FR-10 | AC-10 |
| 审计全覆盖后容灾：R5 工具未注入 audit 时会否破坏存量 SAFE 测试 | 采用：实现期以测试实证；若破坏则装配期 static_check 阻断（contract Open 已登记） | FR-6 | AC-3 / 全量 pytest |

## 模式切换红队复审（假设规格有漏，逐维找洞）
- 维度1 需求遗漏：四项原话逐条在矩阵，S1~S7 拷问发现全登记（S2 显式 Open）；✅
- 维度2 契约冲突：R9:199"同源" vs R11:167"独立"对撞 → 用户裁决同源，文档须同步；✅
- 维度3 硬约束：R1 零改动 / install 协议签名 / 零桩 → Invariants + AC-5/AC-7/AC-9 覆盖；✅
- 维度4 失败路径：审计缺席 fail-open / CRITICAL 无沙盒 / 审批超时 → FR-4/FR-6 与 ApprovingHumanPort fail-closed 保留；✅
- 维度5 可验证性：每条 FR 有命令/退出码/测试名验收，无主观"看起来对了"；✅
- 维度6 被否决方案复活：Open 段登记 + removal_check + ast_scan 防复辟；✅
