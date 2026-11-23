# Change: 8ef577c1-injection-wiring / 2026-11-23 / draft

## Why
用户看完全模块接线体检后指出：框架的核心主线是"用户可插拔注入 + 内部自动编排"，实现一个 agent 初始代码应该很少；但现状是注入的东西大量"看起来接了、实际没生效"——存字段不读、默认新建替换、只取 prompt 不调方法、登记名字冒充能力、测试反向固化错误行为。8 个 Blocker 跨 6 大模块群，门禁全绿却主线断裂。用户铁律：禁止兼容、禁止打补丁，否则代码越写越糟。这次按主线根因重构，不逐点打补丁。

## Delta
- 替换 三套注入主体（`api.Agent` / `compose.Agent` / `AgentBuilder`）为单一语义门面 `api.Agent`；`compose.Agent` 的 `rag()/memory()/telemetry()/event_stream()` 死注入字段删除（因统一由门面 capability 注入而消失）。
- 删除 `compose.Agent.rag/memory/telemetry/event_stream`（因死注入而消失）——带符号名，removal_check 核对。
- 删除 `api/builder.py` 的占位名降级分支 `self._caps.setdefault(cap_name, cap_name)`（因禁止登记名冒充能力而消失）。
- 新增 注入即生效门禁：任何 capability/strategy/tool 注入后，装配期必须有真实消费者（port handler 被执行或进入 Program），否则 fail-closed。替换现有"只取 name/install 空转"的伪注入。
- 替换 `ModelCapability`/`ToolsCapability` 的 install 空转 + 硬编码 dispatch 旁路为统一 Capability 协议履约（`compose/registry.py` 的 `Capability`：name/version/requires/install）。
- 新增 `MemoryCapability`（因用户拍板 FR-9）——挂 say 链 before_model recall + session_end 写回钩子；替换 demo 手动 `memory.write/recall/ingest/commit`（因自动编排而消失）。
- 统一 `MemoryStore` 的 write 与 recall 存储通道（FR-17）：写入的记忆必须可召回。
- 替换 `replay_snapshot` 的 `revive()` 假复活为 R1 `restore_checkpoint`（因静默丢状态而消失）。
- 替换 `ResilienceCapability` 全档位占位为真实装配接线，或删除占位（二选一，S2 确认）；签名对齐 Capability 协议。
- 替换 archs 注入策略只取 prompt 为真实调用 `planner.plan()/executor.execute()/reflector.critique()/replanner.replan()`；删除 `recipes.py:310` replan 丢弃补丁。
- 替换 `inject_strategy` 被 `_install_caps` 跳过的逻辑，接进真实决策点。
- 修复 `Router`/`RouterSkill` 的 `text=""`，接 `current_input()` 真实输入。
- 修复门面 `say()` 装配不传 security/approval/audit 导致的默认新建替换（`agent.py:401-404`）。
- 修复组合算子 8 个死算子 + state-port 必 PORT_MISSING：删除死算子或接 state port（S2 确认）；契约测试从 AST 名字空壳改真实行为测试。
- 修复 guardrail 假审批（`action="ask"` 无消费者）+ 沙盒 to_thread 非 OS 隔离（接真审批 / 诚实标注）。
- 修复 Trace/CacheTelemetry 恒 0：span 写真实 duration，命中率接真实 cache 钩子。
- 删除或接线 `RAGPipeline.build()` / `ContextMemoryManager.build()` / `OffloadPolicy` 死代码（因无消费者而消失，或接进自动链）。
- 替换测试反向固化错误行为（replay/Router/strategy 三处）为真实行为期望。
- 删除 `@tool` 无 description + 逐参数 schema 推导缺失：`decorator.py:81-144` 加 description 形参 + 逐参数类型→JSON Schema；修 `register_tool` 丢 schema + gateway 丢 tools_catalog 三链。
- 删除 demo `_SystemPromptLLM` 用户侧包装（因 `Agent(system=...)` 一等参数而消失）；新增 system 参数。
- 替换 `agent.stream` 私有为公开可观测属性 + sink 协议。

## Invariants
- R1（`src/runtime/`）一行不改（D-7 硬约束）；内核只认 Program，零领域语义。
- 依赖方向恒为 工具层(R3~R9) → 组合层(compose) → 内核 R1；R1 零依赖上层。
- 零死代码/兼容层/测试桩/双轨；改契约同次删旧路径，removal_check 零残留。
- 现有测试行为不回归（除被替换的反向固化 defect 断言）。
- 密钥只走 `DEEPSEEK_API_KEY` 环境变量，不落任何文件。
- 每个功能失败测试先行（TDD RED→GREEN），以 verify.py 命令+退出码为证据。

## Affected
- 模块：`src/api/`（门面统一/注入门禁/system/stream）、`src/compose/`（Capability 协议/算子）、`src/archs/`（策略真调/Router）、`src/harness/{memory,model,tools,security,context,observability,rag,durability}/`、`examples/react-chat/main.py`（删胶水）。
- 测试：`tests/r12/`、`tests/r11/`、`tests/r10/`、`tests/r8/`、`tests/r7/`、`tests/r5/`、`tests/r3/`；新增注入即生效实测。
- 设计文档（S5 必同步）：`docs/01-总览与设计/AGENT-总设计.md`、`docs/02-模块详设/Agent/R{2,5,7,8,9,10,11,12,13}-*.md`。
- 需 conductor 编排影响面核实。

## Acceptance
- AC-1~AC-21 对应 `.coverage.md` 逐行；每行为命令+退出码或文件:行号证据。
- AC-3：贴 `Agent(ReAct([tool]), system=...).use(ModelCapability).use(MemoryCapability).say(...)` 5 行真跑输出。
- AC-5：removal_check 零残留 + `source_purity.py check --strict` rc=0。
- 全量门禁：pytest rc=0、mypy --strict rc=0、12 gate_check 全绿、ast_scan PASS、git diff src/runtime/ 空。
- 独立 review Blocker=0。

## Open
- U-1 意图级联 L3/L4 的 task/tier 透传是否本轮（B4 同源）——需用户拍板，默认范围外。
- U-2 ResilienceCapability：真实接线 vs 删除占位——需 S2 确认（用户铁律倾向删除无消费者路径）。
- U-3 死算子（branch/race/loop/map_state/map_effect/fallback/gate/retry + 4 state-port）：真实接线 vs 删除——需 S2 确认（删死代码符合铁律）。
- U-4 沙盒 OS 强隔离：本轮实现真进程隔离 vs 保持 to_thread 并诚实标注——需用户拍板。
