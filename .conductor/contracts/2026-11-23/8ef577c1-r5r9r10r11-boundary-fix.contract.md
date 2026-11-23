# Change: 8ef577c1-R5R9R10R11 / 2026-11-23 / confirmed

## Why
用户先给出四项已知问题（R5 只 LOW 写审计 / 审计链独立不同源 / EventEnvelope seq·ts 合成 / CRITICAL 需 SandboxPort 才执行），要求"根据设计拷问代码，核查各模块逻辑合理性与模块间联通性，找到问题后一起修掉"。三路只读拷问证实四项属实，另发现两个根因级 Blocker：B1 审计事件字段全量错配（registry 写 danger/source/tool/call_id，AuditLogger 读 who/when/action/cost/data_touched/rationale，生产审计恒空壳但链仍验过）；B2 两条装配链平行空转（builder profile 经 SecurityCapability 装三 Port 进 Agent._reg，assemble_runtime 从不读 _reg，自造 sandbox=None，regulated 档CRITICAL 永 NO_SANDBOX）。用户对四处分叉全选推荐：审计委托 R1 EventStore / ts 真墙钟 + seq 对齐 R1 / 修已知+Blocker+可明确修的 Should（S2 after_model 留契约）/ 沙盒从 SecurityCapability 取。

## Delta
- 替换 `src/harness/tools/registry.py::ToolRegistry.call` 的 danger 路由：抽出统一 `_record_audit`（LOW/HIGH/CRITICAL 三分支全覆盖），写审计事件改用 AuditLogger 实际消费的具名字段（who/when/action/cost/data_touched/rationale），携带 danger/tool/call_id/approved/sandbox/source 语义进 rationale 或 data_touched。删除 LOW 分支独占 record 与 `audit_missing` 静默降级（改为缺席即 fail-closed 或显式装配期校验，按静态检查口径）。
- 替换 `src/harness/security/ports.py::AuditLogger`：`record` 兼容新具名字段并把审计落点委托注入的 R1 `EventStore`（ append-only 序列化进审计专用 session key），保留内存链 + 哈希链供 verify；新增构造期 `sink` 注入口（缺省内存实现，非桩）。
- 新增 `src/api/wiring.py` 的 `R1AuditEventSink`（实现 AuditStore.append）：把审计事件作为 R1 `JsonSnapshot` 写进共享 EventStore 的保留 session（如 `__audit__:<id>`），`replay` 从 R1 store 读回重建 AuditEvent 序列 → 同源可回放；`VerifyingAuditPort` 构造支持注入 sink。删除审计"独立链不同源"表述对应的独立存储路径（内存链降级为热缓存/验链载体，不再唯一底座）。
- 替换 `src/api/wiring.py::bind_stream_projection`：新增 `clock: Callable[[], float] | None` 注入（默认 `time.time` 真实墙钟）与 `seq_provider: Callable[[int], int] | None`；EventEnvelope.ts 用 clock()，seq 优先由 Notice 携带的 R1 `Event.seq`（经 `Notice.seq` 新字段）投影，无 R1 序号时回退 clock 单调源并显式标注合成。R1 内核零改动（`Notice` 属组合层 compose）。
- 新增 `src/compose/notify.py::Notice.seq`（可选 int，默认 0）与 `runner_notifier` 透传：Runner 现有 `step`/事件序号从 projection 带入 → 组合层 DTO 字段，不下沉 R1。
- 替换 `src/api/runtime.py::assemble_runtime` + `AgentBuilder.runtime`：统一单条装配链——从 builder 的 SecurityCapability 取 sandbox/human/audit 注入 ToolRegistry（替代 sandbox=None 与自造 _NoHuman），删除 `_NoHuman` 死占位；接收 builder 的 stream_bus 复用同一 bus（消除 S1 双 bus）；`capability_tools` 同步支持 sandbox 透传。
- 替换 `src/harness/tools/capability.py::ToolsCapability.install` 签名为 `install(container, reg)`（对齐 Capability 协议），保留单参调用的内部兼容由装配层显式传参（不改协议）。
- 替换 `src/harness/security/approval.py::ApprovalFlow` 默认 trigger 与 `ApprovingHumanPort.ask`：R5 路由传 danger=`level.value`（经 ctx.meta 或显式关键字），使 trigger_danger 配置生效（修 S4）。
- 替换 `src/api/wiring.py::CheckpointEventStore`：共享单一 R1 EventStore 实例（audit sink 与 checkpoint 底座同一 store），删除双写第二份 store（修 S7）。
- 同步文档：`docs/02-模块详设/Agent/R5-工具MCP与技能.md`、`R9-安全与HITL.md`、`R10-可观测评测与持久化.md`、`R11-集成接入与治理.md` 的 §11 接线表与诚实边界；更新覆盖审计全覆盖/同源/真实时钟/沙盒接线，删除"仅 LOW 写审计""独立链不同源""seq/ts 合成""CRITICAL 需注入沙盒否则 fail-closed"的过时自述。
- 删除（removal 推导）：`- 删除 runtime.py::_NoHuman 占位类（因沙盒由 SecurityCapability 真实注入而消失）`；`- 删除 registry.py LOW 分支 audit_missing 静默降级语义（因审计全覆盖而消失）`；`- 删除 R11 文档 §11 诚实边界②③④中"仅 LOW/不同源/合成/需注入沙盒"的过时表述（因实现兑现而消失）`。

## Invariants
- R1（src/runtime/）一行不改（D-7）：Event.ts 仍是逻辑时钟（=step），墙钟只在组合层 Notice / R11 适配层注入；Event.seq 由 Runner 内核生成，组合层 DTO 只透传不伪造。
- 依赖方向恒为 工具层(R3~R9) → 组合层(compose) → R1；工具层互不直连；R11 是唯一跨模块装配点。AuditStore sink 实现落 src/api/（装配层），不落 harness/security 内部直连 runtime 存储实现。
- 端口签名以源码为准：AuditPort.record(dict, ctx) async / SandboxPort.run / HumanPort.ask / Capability.install(container, reg)。
- 源码纯净铁律：零死代码/兼容层/测试桩；fake 仅在 tests/；审计全覆盖后审计缺席按 fail-closed 处理，不留静默降级。
- TDD RED→GREEN，证据只认 verify.py 命令+退出码；source_purity --strict + HEAD 基线 rc=0；review Blocker=0。
- 审计哈希链保持 append-only + verify_hash_chain 可验；同源指审计事件以 R1 JsonSnapshot 形态进同一 EventStore，可 replay 重建，不新造事件日志底座。
- 审计事件字段是单一真相源：registry 写入字段 == AuditLogger 消费字段（who/when/action/cost/data_touched/rationale），禁止再出现双套字段名。
- 文档与代码同等交付：四项边界与两个新 Blocker 的状态必须在 R5/R9/R10/R11 详设与总设同步，否则 Blocker。

## Affected
- 源码改动：`src/harness/tools/registry.py`、`src/harness/tools/capability.py`、`src/harness/security/ports.py`、`src/compose/notify.py`、`src/api/wiring.py`、`src/api/runtime.py`、`src/api/builder.py`（若 sandbox 装配需在 builder 层取 SecurityCapability）。
- 源码新增：`src/api/wiring.py` 内新增 R1AuditEventSink 类（不新建模块，保持 R11 单一装配点）。
- 测试改动/新增：`tests/r5/test_routing.py`（或现有审计用例）、`tests/r9/test_wiring.py`（去掉"HIGH 不强制审计"断言）、`tests/r11/test_wiring.py`（同源回放/真实时钟/生产 CRITICAL 全链路，删除手工 sandbox 绕过）、新增 `tests/r11/test_boundary_fix.py` 覆盖全部 FR。
- 设计文档：`docs/02-模块详设/Agent/R5-工具MCP与技能.md`、`R9-安全与HITL.md`、`R10-可观测评测与持久化.md`、`R11-集成接入与治理.md`；`docs/01-总览与设计/AGENT-总设计.md`（若 §R9/R11 有边界自述）。
- 门禁：`tests/tools/gate_check.py`（r5/r9/r10/r11 的 behavior/symbols 增补）、`tests/tools/ast_scan.py`（退役符号 audit_missing/_NoHuman 防复辟）。
- conductor 影响面：审计 sink 走 R1 EventStore（不改 R1）；Notice 加字段属组合层 DTO 变更；install 签名变更触达所有 Capability 装配调用点。

## Acceptance
- AC-1 `pytest tests/r11/test_boundary_fix.py -q` 全绿：四项已知 + B1/B2 各有断言（审计三级别写入/同源 replay/真实 ts/生产 CRITICAL 沙盒执行/字段填充非空/装配链合一）。
- AC-2 `pytest tests/r9/test_wiring.py -q`、`tests/r11/test_wiring.py -q` 全绿；"HIGH 不强制审计"旧断言已删除；手工 sandbox 绕过改为生产装配断言。
- AC-3 R5 路由：LOW/HIGH/CRITICAL 三分支均有 audit.record 调用；审计缺席 fail-closed；字段写入与 AuditLogger 消费一致（无空壳）。
- AC-4 审计同源：审计事件以 JsonSnapshot 形态落 R1 EventStore 保留 session；从同一 store replay 重建 AuditEvent 序列且 verify_hash_chain=True；内存链不再是唯一底座。
- AC-5 事件流：EventEnvelope.ts 来自注入 clock（测试注入固定时钟断言精确值，不再 float(n-1)）；seq 优先取 Notice 携带的 R1 序号；R1 Event.ts=step 与 src/runtime/ 零改动。
- AC-6 装配链：assemble_runtime 消费 SecurityCapability 的 sandbox/human/audit；profile("regulated") 后生产路径 CRITICAL 审批通过真执行；_NoHuman 零残留；stream_bus 与 Runner.notify 同一 bus。
- AC-7 Should 修复：S1 单 bus / S3 fail-closed / S4 trigger_danger 生效 / S5 install 两参 / S7 单 store 各有断言；S2 after_model 留功能接线契约并标注（不造假实现）。
- AC-8 `pytest tests/ -q` 全量退出 0；`mypy --strict src` rc=0；`gate_check.py --module r5|r9|r10|r11` 全绿；`ast_scan.py` 对应模块 PASS。
- AC-9 source_purity check --strict + HEAD 基线 rc=0（新增污染 0）；R1 零改动（`git diff src/runtime/` 空）。
- AC-10 文档同步：R5/R9/R10/R11 详设 + 总设相关段过期自述清零，0 死链；独立 review Blocker=0；review_gate rc=0。
- AC-11 被否决方案不复活：审计独立内存链作唯一底座 / 合成 ts / sandbox=None 默认 / install 单参；removal_check 对 _NoHuman、audit_missing、旧边界表述零残留。

## Open
- S2 after_model 生产触发：ModelGateway 无生产调用者（agent_runtime.model_gateway() 仅 getter）。按用户选择留功能接线契约 + 文档标注"由 R11 在真实 LLM 装配时调用 `AgentRuntime.model_gateway(llm)` 接入 AFTER_MODEL 相位"，本轮不造假实现。
- 中段 fail-closed 语义：审计缺席在 R5 路由处 fail-closed（返回 error + audit_missing 标记），需确认现有 SAFE 工具测试无审计注入不受影响——实现期以测试实证，若破坏存量则改为装配期 static_check 阻断（登记为 contract 微调，不静默放宽）。
